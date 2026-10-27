"""Runtime control: port assignment, protocol and traffic control.

Chassis are shared between IxNetwork VMs (other projects' sessions own ports on the same
AresONE), so assign_ports never takes a port owned by another session unless the target's
guards.allow_port_takeover is set AND the call is confirmed.
"""

from __future__ import annotations

import time
from typing import Any

from . import capture, rest
from .errors import GUARD_BLOCKED, VALIDATION, IxToolError
from .refs import ConfigIndex

POLL_S = 2.0
LINK_OK = "connectedLinkUp"


# ---------------------------------------------------------------------------- ports

def _chassis_ports(ixn: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """location -> chassis port entry (with owner), plus the registered locations."""
    locs = rest.get(ixn, "locations?includes=hostname,stateV2")
    ports: dict[str, dict[str, Any]] = {}
    for loc in locs:
        for p in rest.get(ixn, f"locations/{loc['id']}/ports?includes=location,owner,isAvailable,isLinkUp,description"):
            ports[p["location"]] = p
    return ports, locs


def _hostname(location: str) -> str:
    return location.split("/")[0].split(";")[0]


def _my_owner(vports: list[dict[str, Any]], chassis: dict[str, dict[str, Any]]) -> set[str]:
    """Owner strings this session already holds (from its own connected vports)."""
    return {chassis[v["location"]]["owner"] for v in vports
            if v.get("location") in chassis and chassis[v["location"]].get("owner")
            and v.get("connectionState", "").startswith("connected")}


def _wait_ports(ixn: Any, hrefs: list[str], wait_s: float) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        states = {v["id"]: v for v in rest.get(ixn, "vport?includes=name,location,connectionState,connectionStatus")}
        wanted = {h: states.get(int(h.rsplit("/", 1)[1])) for h in hrefs}
        if all(s and s.get("connectionState") == LINK_OK for s in wanted.values()) or time.monotonic() >= deadline:
            return {h: s or {} for h, s in wanted.items()}
        time.sleep(POLL_S)


def assign_ports(ixn: Any, mappings: list[dict[str, str]], *, create_missing: bool, force_ownership: bool,
                 allow_takeover: bool, wait_link_up_s: float) -> dict[str, Any]:
    if not mappings:
        raise IxToolError(VALIDATION, "no mappings given; use [{'vport': name, 'location': 'chassis/card.port'}]")
    root = rest.root(ixn)
    index = ConfigIndex.build(ixn, props=["name"], topologies=False, traffic=False)
    vports = rest.get(ixn, "vport?includes=name,location,connectionState")
    chassis, locs = _chassis_ports(ixn)
    registered = {loc.get("hostname") for loc in locs}
    for m in mappings:  # make sure every chassis is known before looking up its ports
        host = _hostname(str(m.get("location", "")))
        if host and host not in registered:
            ixn._connection._create(f"{root}/locations", {"hostname": host})
            registered.add(host)
            time.sleep(POLL_S)
    if registered != {loc.get("hostname") for loc in locs}:
        chassis, locs = _chassis_ports(ixn)
    mine = _my_owner(vports, chassis)

    errors, plan = [], []
    for i, m in enumerate(mappings):
        vref, loc = m.get("vport"), m.get("location")
        if not vref or not loc:
            errors.append(f"mappings[{i}]: needs 'vport' and 'location'")
            continue
        entry = chassis.get(loc)
        if entry is None:
            near = [l for l in chassis if l.startswith(_hostname(loc))][:12]
            errors.append(f"mappings[{i}]: no chassis port '{loc}'" + (f" (known: {near})" if near else ""))
            continue
        owner = entry.get("owner") or ""
        taken = bool(owner) and owner not in mine
        if taken and not force_ownership:
            errors.append(f"mappings[{i}]: {loc} is owned by another session ({owner})")
            continue
        if taken and not allow_takeover:
            raise IxToolError(GUARD_BLOCKED, f"{loc} is owned by {owner}; taking it over is disabled for this target",
                              hint="Another project may be using this port. Set guards.allow_port_takeover=true "
                                   "only if the user confirms the port may be taken.")
        vp = index.by_name.get(vref)
        vp = next((n for n in vp or [] if n.type == "vport"), None)
        if vp is None and not create_missing:
            errors.append(f"mappings[{i}]: no vport named '{vref}' (create_missing=false)")
            continue
        plan.append((vref, vp.href if vp else None, loc, taken))
    if errors:
        raise IxToolError(VALIDATION, f"{len(errors)} problem(s); no port was changed", errors=errors,
                          hint="list_ports(include_chassis=true) shows chassis ports and their owners.")

    hrefs, takeover = [], []
    for vref, href, loc, taken in plan:
        if href is None:
            from .compiler import href_of
            href = href_of(ixn._connection._create(f"{root}/vport", {"name": vref}))
        ixn._connection._update(href, {"location": loc})
        hrefs.append(href)
        if taken:
            takeover.append(href)
    if takeover:
        ixn._connection._execute(f"{root}/vport/operations/connectports", {"arg1": takeover, "arg2": True})
    states = _wait_ports(ixn, hrefs, wait_link_up_s)
    rows, warnings = [], []
    for href in hrefs:
        s = states[href]
        rows.append({"vport": s.get("name"), "location": s.get("location"), "state": s.get("connectionState")})
        if s.get("connectionState") != LINK_OK:
            warnings.append(f"{s.get('name')}: {s.get('connectionState')} {s.get('connectionStatus') or ''}".strip()
                            + " — check cabling / DUT port, or the port's L1 speed (get_node on the vport's l1Config)")
    return {"ports": rows, "warnings": warnings}


def unassign_ports(ixn: Any, vports: list[str]) -> dict[str, Any]:
    index = ConfigIndex.build(ixn, props=["name"], topologies=False, traffic=False)
    nodes = [index.resolve(v, types=["vport"]) for v in vports]
    root = rest.root(ixn)
    # arg2=False keeps the vports (and the topologies bound to them); True would delete them
    ixn._connection._execute(f"{root}/vport/operations/unassignports", {"arg1": [n.href for n in nodes], "arg2": False})
    states = {v["id"]: v for v in rest.get(ixn, "vport?includes=name,connectionState")}
    return {"ports": [{"vport": n.name, "state": states.get(int(n.href.rsplit("/", 1)[1]), {}).get("connectionState")}
                      for n in nodes]}


# ---------------------------------------------------------------------------- protocols

def control_protocols(ixn: Any, action: str, scope: str | None, wait_s: float, devices: str | None = None,
                      schema: Any = None) -> dict[str, Any]:
    root = rest.root(ixn)
    target = None
    if scope:
        target = ConfigIndex.build(ixn, props=["name", "count"]).resolve(scope)
    if devices is not None:
        return _control_devices(ixn, action, target, devices, wait_s, schema)
    if action == "apply_changes":
        before = rest.get(ixn, "globals/topology?includes=applyOnTheFlyState").get("applyOnTheFlyState")
        if before != "allowed":
            return {"action": action, "applied": False, "apply_on_the_fly": before,
                    "note": "nothing to apply" if before == "nothingToApply" else "not allowed in the current state"}
        ixn._connection._execute(f"{root}/globals/topology/operations/applyonthefly",
                                 {"arg1": f"{root}/globals/topology"})
        return {"action": action, "applied": True,
                "apply_on_the_fly": rest.get(ixn, "globals/topology?includes=applyOnTheFlyState").get("applyOnTheFlyState")}
    if target is None:
        if action == "restart_down":
            raise IxToolError(VALIDATION, "restart_down needs a scope (a protocol object, device group or topology)")
        op = {"start": "startallprotocols", "stop": "stopallprotocols"}[action]
        ixn._connection._execute(f"{root}/operations/{op}", {"arg1": "sync"})
    else:
        op = {"start": "start", "stop": "stop", "restart_down": "restartdown"}[action]
        ixn._connection._execute(f"{target.href}/operations/{op}", {"arg1": [target.href]})
    return {"action": action, "scope": target.name if target else "all", **_wait_protocols(ixn, scope, action, wait_s)}


DEVICE_OPS = {"start": "start", "stop": "stop", "restart_down": "restartDown"}
DEVICE_TARGET_STATE = {"start": {"up"}, "restart_down": {"up"}, "stop": {"notStarted"}}


def _control_devices(ixn: Any, action: str, target: Any, devices: str, wait_s: float, schema: Any) -> dict[str, Any]:
    """start / stop / restart_down for some device indices of one device group or protocol object,
    via the operations' sessionIndices overload."""
    from .refs import parse_device_ranges

    if action not in DEVICE_OPS:
        raise IxToolError(VALIDATION, f"devices cannot be combined with action '{action}'")
    if target is None:
        raise IxToolError(VALIDATION, "devices needs a scope: one device group or protocol object")
    op = DEVICE_OPS[action]
    if schema is not None:
        from .compiler import resolve_operation

        _, overloads = resolve_operation(schema, target.type_path, op)
        if not any(a.get("name") == "sessionIndices" for o in overloads for a in o.get("args") or []):
            raise IxToolError(VALIDATION, f"{target.type} cannot {action.replace('_', ' ')} single devices",
                              hint="Scope a device group (start/stop) or a protocol object such as an "
                                   "ethernet, ipv4 or BGP peer (start/stop/restart_down).")
    indices = parse_device_ranges(devices, target.props.get("count"))
    ixn._connection._execute(f"{target.href}/operations/{op}", {"arg1": [target.href], "sessionIndices": indices})
    want = DEVICE_TARGET_STATE[action]
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        states = _device_states(ixn, target.href, indices)
        settled = all(v in want for v in states.values())
        if settled or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)
    from .tools.status import ranges

    by_state: dict[str, list[int]] = {}
    for i, v in states.items():
        by_state.setdefault(v, []).append(i)
    return {"action": action, "scope": target.name, "devices": ranges(indices), "settled": settled,
            "device_states": {k: ranges(v) for k, v in by_state.items()}}


def _device_states(ixn: Any, href: str, indices: list[int]) -> dict[int, str]:
    """Per-device session state of a protocol object, or of a device group (its stack objects
    that have one session per device: up / notStarted / down, or 'mixed')."""
    from .tools.discovery import STACK_REGEX

    node = rest.select(ixn, href, properties=["count", "sessionStatus"],
                       children=[rest.child(STACK_REGEX, ["count", "sessionStatus"])])[0]
    lists: list[list[Any]] = [node["sessionStatus"]] if node.get("sessionStatus") else []
    if not lists:
        def walk(n: dict[str, Any]) -> None:
            for kid in rest.child_nodes(n):
                ss = kid.get("sessionStatus")
                if ss and len(ss) == node.get("count"):
                    lists.append(ss)
                walk(kid)
        walk(node)
    out: dict[int, str] = {}
    for i in indices:
        seen = {str(lst[i - 1]) for lst in lists if i - 1 < len(lst)}
        out[i] = seen.pop() if len(seen) == 1 else ("mixed" if seen else "unknown")
    return out


def _wait_protocols(ixn: Any, scope: str | None, action: str, wait_s: float) -> dict[str, Any]:
    from .tools.status import status as protocol_status  # tools package imports this module

    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        st = protocol_status(ixn, scope, False, False)
        totals = {"up": 0, "down": 0, "not_started": 0, "total": 0}
        for agg in st["summary_by_type"].values():
            for k in totals:
                totals[k] += agg[k]
        settled = (totals["up"] == totals["total"]) if action in ("start", "restart_down") else \
            (totals["not_started"] == totals["total"])
        if settled or time.monotonic() >= deadline:
            problems = [n for n in protocol_status(ixn, scope, False, True)["nodes"]]
            return {"settled": settled, "sessions": totals, "by_type": st["summary_by_type"],
                    "problems": problems[:20] or None}
        time.sleep(POLL_S)


# ---------------------------------------------------------------------------- traffic

TRAFFIC_RUNNING = {"started", "startedWaitingForStats", "startedWaitingForStreams", "txStopWatchExpected"}


def _traffic_state(ixn: Any) -> str:
    return rest.get(ixn, "traffic?includes=state").get("state")


def recent_app_errors(ixn: Any, limit: int = 6) -> list[str]:
    """Latest kError/kWarning entries of IxNetwork's application error log (explains e.g. why an
    apply failed; the REST error itself only says 'Error in L2/L3 Traffic Apply')."""
    try:
        rows = rest.get(ixn, "globals/appErrors/error?includes=name,errorLevel,description")
    except Exception:  # noqa: BLE001
        return []
    out = [f"{r.get('errorLevel')}: {r.get('name')} - {r.get('description')}" for r in rows
           if r.get("errorLevel") in ("kError", "kWarning") and "memory is very low" not in (r.get("name") or "")]
    return [" ".join(x.split())[:300] for x in out[-limit:]]


def control_traffic(ixn: Any, action: str, items: list[str] | None, wait_s: float) -> dict[str, Any]:
    try:
        return _control_traffic(ixn, action, items, wait_s)
    except IxToolError:
        raise
    except Exception as exc:
        from .errors import map_exception

        err = map_exception(exc)
        details = recent_app_errors(ixn)
        hint = None
        if any("destination MAC" in d or "No Flow Groups" in d for d in details):
            hint = ("No packets could be built because destination MACs are unresolved: start protocols "
                    "(control_protocols) so ARP/ND completes, then generate and apply again.")
        raise IxToolError(err.code, err.message, hint or err.hint, server_errors=details or None,
                          **{k: v for k, v in err.extra.items() if k != "server_warnings"}) from exc


def _control_traffic(ixn: Any, action: str, items: list[str] | None, wait_s: float) -> dict[str, Any]:
    root = rest.root(ixn)
    traffic = f"{root}/traffic"
    index = ConfigIndex.build(ixn, props=["name"], topologies=False, ports=False)
    all_items = [n for n in index.nodes if n.type == "trafficItem"]
    chosen = [index.resolve(i, types=["trafficItem"]) for i in items] if items else all_items
    hrefs = [n.href for n in chosen]
    if action in ("generate", "apply", "start") and not hrefs:
        raise IxToolError(VALIDATION, "there are no traffic items; create some with configure_traffic_items")
    before = _traffic_state(ixn)
    if action == "generate":
        if before in TRAFFIC_RUNNING:
            raise IxToolError(VALIDATION, "stop traffic before regenerating it")
        ixn._connection._execute(f"{traffic}/trafficItem/operations/generate", {"arg1": hrefs})
    elif action == "apply":
        if before in TRAFFIC_RUNNING:
            raise IxToolError(VALIDATION, "traffic is running; stop it before applying")
        stopped_captures = capture.running_data_captures(ixn)
        ixn._connection._execute(f"{traffic}/operations/apply", {"arg1": traffic})
    elif action == "start":
        if before == "unapplied" or before == "error":
            ixn._connection._execute(f"{traffic}/trafficItem/operations/generate", {"arg1": hrefs})
            ixn._connection._execute(f"{traffic}/operations/apply", {"arg1": traffic})
        if items:
            ixn._connection._execute(f"{traffic}/operations/startstatelesstrafficblocking", {"arg1": hrefs})
        else:
            ixn._connection._execute(f"{traffic}/operations/start", {"arg1": traffic})
    elif action == "stop":
        if items:
            ixn._connection._execute(f"{traffic}/operations/stopstatelesstrafficblocking", {"arg1": hrefs})
        else:
            ixn._connection._execute(f"{traffic}/operations/stop", {"arg1": traffic})
    elif action == "clear_stats":
        ixn._connection._execute(f"{root}/operations/clearstats", {})
    else:
        raise IxToolError(VALIDATION, f"unknown action '{action}'")
    target_states = {"start": TRAFFIC_RUNNING, "stop": {"stopped"}}.get(action)
    deadline = time.monotonic() + max(0.0, wait_s)
    state = _traffic_state(ixn)
    while target_states and state not in target_states and time.monotonic() < deadline:
        time.sleep(POLL_S)
        state = _traffic_state(ixn)
    rows = rest.get(ixn, "traffic/trafficItem?includes=name,state,errors")
    rows = [r for r in rows if not items or r["name"] in {n.name for n in chosen}]
    out: dict[str, Any] = {
        "action": action, "traffic_state_before": before, "traffic_state": state,
        "items": [{"name": r["name"], "state": r.get("state"), **({"errors": r["errors"]} if r.get("errors") else {})}
                  for r in rows]}
    if action == "apply" and stopped_captures:
        out["warnings"] = [f"Apply stopped the data capture on {stopped_captures}; start it again with "
                           "control_capture('start') before starting traffic"]
    if action in ("apply", "start") and any(r.get("state") == "unapplied" for r in rows):
        out["note"] = ("items still 'unapplied' have no packets yet, usually because destination MACs are not "
                       "resolved: start protocols (control_protocols), then control_traffic('generate') and apply")
    return out
