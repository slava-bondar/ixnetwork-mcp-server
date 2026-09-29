"""Create / update / read traffic items.

Server quirks handled here:
* TransmissionControl: set Type in its own PATCH before type-specific fields
  (sending both at once makes the server throw NullReferenceException when switching types)
* stack layers: remove non-base layers one at a time with a fresh Stack.find() (hrefs go
  stale after each removal); never use the href returned by Append - re-read the stack
* fields: Auto=False before ValueType and the value

Endpoints are resolved through ConfigIndex by exact name (restpy's find() uses unanchored
regexes, so "SITE-1" would also match "SITE-10"); protocol templates are read once per call; an
unknown template / field / tracking option is an error with candidates, and a failed create
removes the half-built item.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from .. import rest
from ..refs import ConfigIndex, Node
from .models import (ConfigElementConfig, EndpointRef, EndpointSetConfig, FlowTrackingConfig, FrameSizeType,
                     TrafficItemConfig, TransmissionType)

log = logging.getLogger("ixnetwork_mcp.traffic")

_BASE_LAYER_KEYWORDS = ("ethernet", "ipv4", "ipv6", "vlan", "fcs", "frame check sequence")
EXPECTED_GENERATE_WARNINGS = ("destination MACs or VPNs are invalid or unreachable",
                              "no packets were generated due to destination MACs")


class TrafficProblems(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _anchored(name: str) -> str:
    return "^" + re.escape(name) + "$"


def _close(name: str, choices: list[str], n: int = 8) -> list[str]:
    return difflib.get_close_matches(name, choices, n=n, cutoff=0.4) or choices[:n]


# ---------------------------------------------------------------------------- endpoints

def _under(node: Node, ancestor: Node) -> bool:
    return node.xpath.startswith(ancestor.xpath + "/")


def resolve_endpoint(index: ConfigIndex, ref: EndpointRef, where: str) -> tuple[str | None, str | None]:
    """-> (href, error). Raw (vport) endpoints are the vport's /protocols object."""
    from ..errors import IxToolError

    def by(type_: str, name: str, within: Node | None = None) -> list[Node]:
        return [n for n in index.by_name.get(name, []) if n.type == type_ and (within is None or _under(n, within))]

    try:
        if ref.ref is not None:
            node = index.resolve(ref.ref)
            return (f"{node.href}/protocols" if node.type == "vport" else node.href), None
        if ref.vport is not None:
            node = index.resolve(ref.vport, types=["vport"])
            return f"{node.href}/protocols", None
        topo = None
        if ref.topology is not None:
            t = by("topology", ref.topology)
            if not t:
                return None, f"{where}: no topology named '{ref.topology}'"
            topo = t[0]
        if ref.device_group is not None:
            dgs = by("deviceGroup", ref.device_group, topo)
            if not dgs:
                return None, f"{where}: no device group '{ref.device_group}'" + (f" in '{ref.topology}'" if topo else "")
            if ref.network_group is not None:
                ngs = by("networkGroup", ref.network_group, dgs[0])
                if not ngs:
                    return None, f"{where}: no network group '{ref.network_group}' under '{ref.device_group}'"
                return ngs[0].href, None
            return dgs[0].href, None
        if ref.network_group is not None:
            ngs = by("networkGroup", ref.network_group, topo)
            return (ngs[0].href, None) if ngs else (None, f"{where}: no network group '{ref.network_group}'")
        return (topo.href, None) if topo else (None, f"{where}: empty endpoint")
    except IxToolError as exc:
        cands = exc.extra.get("candidates")
        return None, f"{where}: {exc.message}" + (f" (candidates: {[c.get('name') for c in cands]})" if cands else "")


def resolve_endpoint_sets(index: ConfigIndex, cfg: TrafficItemConfig) -> list[dict[str, Any]]:
    resolved, problems = [], []
    for i, es in enumerate(cfg.endpoint_sets):
        srcs = [resolve_endpoint(index, r, f"{cfg.name}.endpoint_sets[{i}].sources[{j}]") for j, r in enumerate(es.sources)]
        dsts = [resolve_endpoint(index, r, f"{cfg.name}.endpoint_sets[{i}].destinations[{j}]")
                for j, r in enumerate(es.destinations)]
        problems += [e for _, e in srcs + dsts if e]
        entry: dict[str, Any] = {"Sources": [h for h, _ in srcs if h]}
        if es.multicast_destinations:
            v6 = ":" in es.multicast_destinations[0].group
            entry["MulticastDestinations"] = [
                {"arg1": False, "arg2": "none", "arg3": d.group, "arg4": d.step or ("::" if v6 else "0.0.0.0"),
                 "arg5": d.count} for d in es.multicast_destinations]
        else:
            entry["Destinations"] = [h for h, _ in dsts if h]
        resolved.append(entry)
    if problems:
        raise TrafficProblems(problems)
    return resolved


# ---------------------------------------------------------------------------- templates / stack

class Templates:
    def __init__(self, ixn: Any):
        root = rest.root(ixn)
        rows = rest.get(ixn, "traffic/protocolTemplate?includes=displayName,stackTypeId")
        self.rows = [{"href": f"{root}/traffic/protocolTemplate/{r['id']}", "display": r.get("displayName") or "",
                      "type": r.get("stackTypeId") or ""} for r in rows]

    def find(self, name: str) -> tuple[dict[str, Any] | None, list[str]]:
        n = name.lower()
        for key in ("type", "display"):
            exact = [r for r in self.rows if r[key].lower() == n]
            if exact:
                return exact[0], []
        partial = [r for r in self.rows if n in r["display"].lower()]
        if len(partial) == 1:
            return partial[0], []
        return None, [r["type"] for r in partial[:8]] or _close(name, [r["type"] for r in self.rows])


def _is_base_layer(display_name: str) -> bool:
    dn = (display_name or "").lower()
    return any(kw in dn for kw in _BASE_LAYER_KEYWORDS)


def _match_stack(stacks: list[Any], protocol: str) -> Any | None:
    p = protocol.lower()
    for s in stacks:
        if (s.StackTypeId or "").lower() == p:
            return s
    for s in stacks:
        if p in (s.DisplayName or "").lower():
            return s
    return None


def _apply_stack_fields(stack_obj: Any, header, where: str, problems: list[str]) -> None:
    fields = list(stack_obj.Field.find())
    for fcfg in header.fields:
        want = fcfg.field_name.lower()
        f = next((x for x in fields if (x.DisplayName or "").lower() == want), None) or \
            next((x for x in fields if (x.FieldTypeId or "").lower() == want), None) or \
            next((x for x in fields if (x.FieldTypeId or "").lower().endswith("." + want)), None)
        if f is None:
            problems.append(f"{where}.fields: no field '{fcfg.field_name}' in {stack_obj.DisplayName} "
                            f"(e.g. {_close(fcfg.field_name, [x.DisplayName for x in fields])})")
            continue
        f.Auto = False
        f.ValueType = fcfg.value_type
        if fcfg.value_type == "singleValue" and fcfg.single_value is not None:
            f.SingleValue = fcfg.single_value
        elif fcfg.value_type in ("increment", "decrement"):
            if fcfg.start_value is not None:
                f.StartValue = fcfg.start_value
            if fcfg.step_value is not None:
                f.StepValue = fcfg.step_value
            if fcfg.count is not None:
                f.CountValue = fcfg.count
        elif fcfg.value_type == "valueList" and fcfg.value_list is not None:
            f.ValueList = fcfg.value_list


def apply_config_element(ce: Any, cfg: ConfigElementConfig, templates: Templates, where: str,
                         problems: list[str]) -> None:
    ce.FrameRate.update(Type=cfg.frame_rate.type.value, Rate=cfg.frame_rate.rate)
    fs = ce.FrameSize
    fs.Type = cfg.frame_size.type.value
    if cfg.frame_size.type == FrameSizeType.FIXED:
        fs.FixedSize = cfg.frame_size.fixed_size
    elif cfg.frame_size.type == FrameSizeType.INCREMENT:
        if cfg.frame_size.min_size is not None:
            fs.IncrementFrom = cfg.frame_size.min_size
        if cfg.frame_size.max_size is not None:
            fs.IncrementTo = cfg.frame_size.max_size
        if cfg.frame_size.step_size is not None:
            fs.IncrementStep = cfg.frame_size.step_size
    elif cfg.frame_size.type == FrameSizeType.RANDOM:
        if cfg.frame_size.min_size is not None:
            fs.RandomMin = cfg.frame_size.min_size
        if cfg.frame_size.max_size is not None:
            fs.RandomMax = cfg.frame_size.max_size

    tc = cfg.transmission
    kw: dict[str, Any] = {}
    if tc.type == TransmissionType.FIXED_FRAME_COUNT and tc.frame_count is not None:
        kw["FrameCount"] = tc.frame_count
    elif tc.type == TransmissionType.FIXED_DURATION and tc.duration is not None:
        kw["Duration"] = tc.duration
    elif tc.type in (TransmissionType.BURST_AUTO, TransmissionType.BURST_CUSTOM, TransmissionType.BURST_FIXED_DURATION):
        if tc.min_gap_bytes is not None:
            kw["MinGapBytes"] = tc.min_gap_bytes
        if tc.inter_burst_gap is not None:
            kw.update(EnableInterBurstGap=True, InterBurstGap=tc.inter_burst_gap,
                      InterBurstGapUnits=tc.inter_burst_gap_units)
        if tc.type in (TransmissionType.BURST_CUSTOM, TransmissionType.BURST_FIXED_DURATION) and \
                tc.burst_packet_count is not None:
            kw["BurstPacketCount"] = tc.burst_packet_count
        if tc.type == TransmissionType.BURST_FIXED_DURATION and tc.repeat_burst is not None:
            kw["RepeatBurst"] = tc.repeat_burst
    ce.TransmissionControl.update(Type=tc.type.value)  # separate PATCH (see module docstring)
    if kw:
        ce.TransmissionControl.update(**kw)

    if not cfg.stack:
        return
    wanted = []
    for i, header in enumerate(cfg.stack):
        tpl, cands = templates.find(header.protocol)
        if tpl is None:
            problems.append(f"{where}.stack[{i}]: unknown protocol '{header.protocol}' (e.g. {cands})")
        wanted.append((header, tpl))
    if any(t is None for _, t in wanted):
        return
    while True:  # wipe non-base layers, one at a time with a fresh find()
        stale = next((s for s in ce.Stack.find() if not _is_base_layer(s.DisplayName or "")), None)
        if stale is None:
            break
        stale.Remove()
    for header, tpl in wanted:
        if _is_base_layer(tpl["display"]) and _match_stack(list(ce.Stack.find()), header.protocol):
            continue  # base layers (ethernet/vlan/ipv4/ipv6) already exist
        stacks = list(ce.Stack.find())
        insert_at = next((s for s in reversed(stacks) if "frame check sequence" not in (s.DisplayName or "").lower()
                          and "fcs" not in (s.DisplayName or "").lower()), stacks[-1])
        insert_at.Append(Arg2=tpl["href"])
    stacks = list(ce.Stack.find())
    for i, (header, _) in enumerate(wanted):
        s = _match_stack(stacks, header.protocol)
        if s is None:
            problems.append(f"{where}.stack[{i}]: '{header.protocol}' is not in the packet after adding it")
            continue
        _apply_stack_fields(s, header, f"{where}.stack[{i}]", problems)


def apply_tracking(ti: Any, cfg: FlowTrackingConfig, where: str, problems: list[str]) -> None:
    tr = ti.Tracking.find()[0]
    available = list(tr.AvailableTrackBy or [])
    bad = [t for t in cfg.track_by if available and t not in available]
    if bad:
        problems.append(f"{where}.flow_tracking.track_by: unknown {bad} (available e.g. "
                        f"{_close(bad[0], available)})")
        return
    track = list(cfg.track_by)
    if track and "trackingenabled0" not in track:
        track.insert(0, "trackingenabled0")  # tracking does nothing without it
    tr.TrackBy = track
    tr.FieldWidth = cfg.field_width
    if cfg.protocol_offset is not None:
        tr.ProtocolOffset = cfg.protocol_offset
    if cfg.offset is not None:
        tr.Offset = cfg.offset
    if cfg.egress is not None:
        eg = tr.Egress
        eg.Enabled = cfg.egress.enabled
        for attr, val in (("Encapsulation", cfg.egress.encapsulation), ("FieldOffset", cfg.egress.field_offset),
                          ("CustomOffsetBits", cfg.egress.custom_offset_bits),
                          ("CustomWidthBits", cfg.egress.custom_width_bits)):
            if val is not None:
                setattr(eg, attr, val)


def _set_endpoint_sets(ti: Any, resolved: list[dict[str, Any]]) -> None:
    for ep in list(ti.EndpointSet.find()):
        ep.remove()
    for entry in resolved:
        ti.EndpointSet.add(**entry)


def _finish(ti: Any, cfg: TrafficItemConfig, templates: Templates) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    for idx, ce in enumerate(ti.ConfigElement.find()):
        apply_config_element(ce, cfg.config_element_for(idx), templates, f"{cfg.name}.config_elements[{idx}]", problems)
    apply_tracking(ti, cfg.flow_tracking, cfg.name, problems)
    if problems:
        return problems, []
    ti.Generate()
    ti.refresh()
    errors = list(ti.Errors or [])
    warnings = [w for w in ti.Warnings or []]
    return [f"{cfg.name}: {e}" for e in errors], warnings


# ---------------------------------------------------------------------------- public

def find_item(ixn: Any, name: str) -> Any | None:
    found = ixn.Traffic.TrafficItem.find(Name=_anchored(name))
    return found if len(found) == 1 else None


def create(ixn: Any, cfg: TrafficItemConfig, index: ConfigIndex, templates: Templates) -> dict[str, Any]:
    resolved = resolve_endpoint_sets(index, cfg)  # raises before anything is created
    ti = ixn.Traffic.TrafficItem.add(Name=cfg.name, TrafficType=cfg.traffic_type.value,
                                     BiDirectional=cfg.bi_directional, SrcDestMesh=cfg.src_dest_mesh,
                                     AllowSelfDestined=cfg.self_destined, Enabled=cfg.enabled)
    try:
        for entry in resolved:
            ti.EndpointSet.add(**entry)
        errors, warnings = _finish(ti, cfg, templates)
        if errors:
            raise TrafficProblems(errors)
    except Exception:
        try:
            ti.remove()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove half-built traffic item %s: %s", cfg.name, exc)
        raise
    return _outcome(ti, warnings, "created")


def update(ixn: Any, cfg: TrafficItemConfig, ti: Any, index: ConfigIndex, templates: Templates) -> dict[str, Any]:
    resolved = resolve_endpoint_sets(index, cfg)
    ti.update(TrafficType=cfg.traffic_type.value, BiDirectional=cfg.bi_directional, SrcDestMesh=cfg.src_dest_mesh,
              AllowSelfDestined=cfg.self_destined, Enabled=cfg.enabled)
    _set_endpoint_sets(ti, resolved)
    errors, warnings = _finish(ti, cfg, templates)
    if errors:
        raise TrafficProblems(errors)
    return _outcome(ti, warnings, "updated")


def _outcome(ti: Any, warnings: list[str], action: str) -> dict[str, Any]:
    unexpected = [w for w in warnings if not any(e in w for e in EXPECTED_GENERATE_WARNINGS)]
    out: dict[str, Any] = {"name": ti.Name, "action": action, "state": ti.State,
                           "flow_groups": len(ti.HighLevelStream.find())}
    if warnings and not unexpected:
        out["note"] = ("destination MACs are not resolved yet (protocols not started / ARP not done): "
                       "regenerate after starting protocols")
    elif unexpected:
        out["warnings"] = unexpected[:10]
    return out


# ---------------------------------------------------------------------------- read

def summarize(ixn: Any, index: ConfigIndex, names: list[str] | None) -> list[dict[str, Any]]:
    root = rest.root(ixn)
    props = ["name", "trafficType", "state", "enabled", "biDirectional", "srcDestMesh", "errors", "warnings",
             "sources", "destinations", "multicastDestinations", "type", "rate", "fixedSize", "trackBy"]
    r = rest.select(ixn, f"{root}/traffic", properties=[], children=[rest.child(
        "^(trafficItem|endpointSet|configElement|frameRate|frameSize|transmissionControl|tracking)$", props)])
    name_of = {n.href.rstrip("/"): n.name for n in index.nodes}

    def label(h: str) -> str:
        h = h.rstrip("/")
        if h.endswith("/protocols"):
            return f"vport:{name_of.get(h[:-len('/protocols')], h)}"
        return name_of.get(h, h)

    out = []
    for ti in r[0].get("trafficItem") or []:
        if names and ti.get("name") not in names:
            continue
        ces = ti.get("configElement") or []
        row = {
            "name": ti.get("name"), "type": ti.get("trafficType"), "state": ti.get("state"),
            "enabled": ti.get("enabled"), "bi_directional": ti.get("biDirectional"), "mesh": ti.get("srcDestMesh"),
            "endpoint_sets": [{"sources": [label(h) for h in es.get("sources") or []],
                               "destinations": [label(h) for h in es.get("destinations") or []],
                               **({"multicast": [m.get("arg3") for m in es.get("multicastDestinations") or []]}
                                  if es.get("multicastDestinations") else {})}
                              for es in ti.get("endpointSet") or []],
            "rate": [{"type": (ce.get("frameRate") or {}).get("type"), "rate": (ce.get("frameRate") or {}).get("rate"),
                      "frame_size": (ce.get("frameSize") or {}).get("fixedSize"),
                      "frame_size_type": (ce.get("frameSize") or {}).get("type"),
                      "transmission": (ce.get("transmissionControl") or {}).get("type")} for ce in ces],
            "tracking": ((ti.get("tracking") or [{}])[0] or {}).get("trackBy"),
        }
        if ti.get("errors"):
            row["errors"] = ti["errors"]
        warns = [w for w in ti.get("warnings") or [] if not any(e in w for e in EXPECTED_GENERATE_WARNINGS)]
        if warns:
            row["warnings"] = warns[:5]
        out.append(row)
    return out


def read_full(ixn: Any, index: ConfigIndex, ti: Any) -> dict[str, Any]:
    """Traffic item as a TrafficItemConfig-shaped dict (edit and resubmit with mode='update')."""
    name_of = {n.href.rstrip("/"): n for n in index.nodes}

    def ref(h: str) -> dict[str, str]:
        h = h.rstrip("/")
        if h.endswith("/protocols"):
            vp = name_of.get(h[:-len("/protocols")])
            return {"vport": vp.name if vp else h}
        n = name_of.get(h)
        return {"ref": n.name if n and n.name else (n.xpath if n else h)}

    eps = []
    for es in ti.EndpointSet.find():
        entry: dict[str, Any] = {"sources": [ref(h) for h in es.Sources or []]}
        if es.MulticastDestinations:
            entry["multicast_destinations"] = [{"group": m.get("arg3"), "step": m.get("arg4"), "count": m.get("arg5")}
                                               for m in es.MulticastDestinations]
        else:
            entry["destinations"] = [ref(h) for h in es.Destinations or []]
        eps.append(entry)
    ces = []
    for ce in ti.ConfigElement.find():
        fs = ce.FrameSize
        frame_size: dict[str, Any] = {"type": fs.Type}
        if fs.Type == "fixed":
            frame_size["fixed_size"] = fs.FixedSize
        elif fs.Type == "increment":
            frame_size.update(min_size=fs.IncrementFrom, max_size=fs.IncrementTo, step_size=fs.IncrementStep)
        elif fs.Type == "random":
            frame_size.update(min_size=fs.RandomMin, max_size=fs.RandomMax)
        tc = ce.TransmissionControl
        transmission = {"type": tc.Type}
        if tc.Type == "fixedFrameCount":
            transmission["frame_count"] = tc.FrameCount
        elif tc.Type == "fixedDuration":
            transmission["duration"] = tc.Duration
        stack = []
        for s in ce.Stack.find():
            # only what someone configured: non-auto fields that differ from their default
            fields = [f for f in s.Field.find() if f.Auto is False and not f.ReadOnly and not
                      (f.ValueType == "singleValue" and str(f.SingleValue) == str(f.DefaultValue))]
            if _is_base_layer(s.DisplayName) and not fields:
                continue
            entry = {"protocol": s.StackTypeId, "fields": []}
            for f in fields:
                fe: dict[str, Any] = {"field_name": f.DisplayName, "value_type": f.ValueType}
                if f.ValueType == "singleValue":
                    fe["single_value"] = f.SingleValue
                elif f.ValueType in ("increment", "decrement"):
                    fe.update(start_value=f.StartValue, step_value=f.StepValue, count=f.CountValue)
                elif f.ValueType == "valueList":
                    fe["value_list"] = f.ValueList
                entry["fields"].append(fe)
            stack.append(entry)
        ces.append({"frame_rate": {"type": ce.FrameRate.Type, "rate": ce.FrameRate.Rate},
                    "frame_size": frame_size, "transmission": transmission, "stack": stack})
    tr = ti.Tracking.find()[0]
    return {"name": ti.Name, "traffic_type": ti.TrafficType, "bi_directional": ti.BiDirectional,
            "self_destined": ti.AllowSelfDestined, "src_dest_mesh": ti.SrcDestMesh, "enabled": ti.Enabled,
            "endpoint_sets": eps, "config_elements": ces,
            "flow_tracking": {"track_by": list(tr.TrackBy or []), "field_width": tr.FieldWidth}}


def packet_headers(ixn: Any, template: str | None, query: str | None) -> dict[str, Any]:
    templates = Templates(ixn)
    if not template:
        q = (query or "").lower()
        rows = [{"protocol": r["type"], "name": r["display"]} for r in templates.rows
                if not q or q in r["type"].lower() or q in r["display"].lower()]
        return {"total": len(rows), "templates": rows[:200]}
    tpl, cands = templates.find(template)
    if tpl is None:
        from ..errors import NOT_FOUND, IxToolError
        raise IxToolError(NOT_FOUND, f"no packet header template '{template}'", candidates=cands)
    fields = rest.get(ixn, tpl["href"].split("/ixnetwork/", 1)[1] +
                      "/field?includes=displayName,fieldTypeId,defaultValue,valueFormat,enumValues,"
                      "availableValueTypes,readOnly,optional")
    return {"protocol": tpl["type"], "name": tpl["display"], "fields": [
        {k: v for k, v in (("field_name", f.get("displayName")), ("id", f.get("fieldTypeId")),
                           ("default", f.get("defaultValue")), ("format", f.get("valueFormat")),
                           ("enums", f.get("enumValues") or None), ("value_types", f.get("availableValueTypes")),
                           ("read_only", f.get("readOnly") or None)) if v not in (None, "", [])}
        for f in fields]}
