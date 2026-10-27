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
from ..errors import IxToolError, map_exception
from ..refs import ConfigIndex, Node
from .models import (ConfigElementConfig, EndpointRef, EndpointSetConfig, FlowTrackingConfig, FrameSizeType,
                     HeaderChange, PacketHeaderField, TrafficItemConfig, TransmissionType)

log = logging.getLogger("ixnetwork_mcp.traffic")

_BASE_LAYER_KEYWORDS = ("ethernet", "ipv4", "ipv6", "vlan", "fcs", "frame check sequence")
_L2_TYPES = ("ethernet", "vlan", "mpls")  # new headers of these types go after the last of them
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


def _endpoint_node(index: ConfigIndex, ref: EndpointRef, where: str) -> tuple[Node | None, str | None, str | None]:
    """-> (node, href, error). Raw (vport) endpoints are the vport's /protocols object."""
    from ..errors import IxToolError

    def by(type_: str, name: str, within: Node | None = None) -> list[Node]:
        return [n for n in index.by_name.get(name, []) if n.type == type_ and (within is None or _under(n, within))]

    try:
        if ref.ref is not None:
            node = index.resolve(ref.ref)
            return node, (f"{node.href}/protocols" if node.type == "vport" else node.href), None
        if ref.vport is not None:
            node = index.resolve(ref.vport, types=["vport"])
            return node, f"{node.href}/protocols", None
        topo = None
        if ref.topology is not None:
            t = by("topology", ref.topology)
            if not t:
                return None, None, f"{where}: no topology named '{ref.topology}'"
            topo = t[0]
        if ref.device_group is not None:
            dgs = by("deviceGroup", ref.device_group, topo)
            if not dgs:
                return None, None, (f"{where}: no device group '{ref.device_group}'"
                                    + (f" in '{ref.topology}'" if topo else ""))
            if ref.network_group is not None:
                ngs = by("networkGroup", ref.network_group, dgs[0])
                if not ngs:
                    return None, None, f"{where}: no network group '{ref.network_group}' under '{ref.device_group}'"
                return ngs[0], ngs[0].href, None
            return dgs[0], dgs[0].href, None
        if ref.network_group is not None:
            ngs = by("networkGroup", ref.network_group, topo)
            return (ngs[0], ngs[0].href, None) if ngs else (None, None, f"{where}: no network group '{ref.network_group}'")
        return (topo, topo.href, None) if topo else (None, None, f"{where}: empty endpoint")
    except IxToolError as exc:
        cands = exc.extra.get("candidates")
        return None, None, f"{where}: {exc.message}" + (f" (candidates: {[c.get('name') for c in cands]})" if cands else "")


def resolve_endpoint(index: ConfigIndex, ref: EndpointRef, where: str) -> tuple[str | None, str | None]:
    """-> (href, error). Raw (vport) endpoints are the vport's /protocols object."""
    _, href, err = _endpoint_node(index, ref, where)
    return href, err


def _subset(index: ConfigIndex, node: Node, ref: EndpointRef, where: str) -> tuple[dict[str, Any] | None, str | None]:
    """Scalable endpoint {arg1: object, arg2: port start, arg3: port count, arg4: first device, arg5: device count}.
    Ports are 1-based positions in the topology's port list; devices are 1-based and per port."""
    m = re.match(r"^/topology\[\d+\]", node.xpath)
    topo = index.by_xpath.get(m.group(0)) if m else None
    if topo is None or "/deviceGroup[" not in node.xpath:
        return None, f"{where}: a port/device subset needs an object inside a device group, not a {node.type}"
    ports = [str(p).rstrip("/") for p in topo.props.get("ports") or []]
    names = [index.by_href[p].name if p in index.by_href else p for p in ports]
    if not ports:
        return None, f"{where}: topology '{topo.name}' has no ports"
    ps = 1 if ref.port_start is None else ref.port_start
    if isinstance(ps, str):
        if ps.strip().isdigit():
            ps = int(ps)
        elif ps in names:
            ps = names.index(ps) + 1
        else:
            return None, f"{where}.port_start: '{ps}' is not a port of topology '{topo.name}' (ports: {names})"
    if not 1 <= ps <= len(ports):
        return None, f"{where}.port_start: {ps} is outside 1..{len(ports)} (topology '{topo.name}' ports: {names})"
    pc = len(ports) - ps + 1 if ref.port_count is None else ref.port_count
    if ps + pc - 1 > len(ports):
        return None, (f"{where}.port_count: {pc} ports from port_start {ps} need {ps + pc - 1}, "
                      f"topology '{topo.name}' has {len(ports)} ({names})")
    try:
        per_port = int(node.props.get("count")) // len(ports)
    except (TypeError, ValueError):
        return None, f"{where}: cannot read the device count of '{node.name}'"
    ds = 1 if ref.device_start is None else ref.device_start
    if ds > per_port:
        return None, f"{where}.device_start: {ds} is outside 1..{per_port} ('{node.name}' has {per_port} per port)"
    dc = per_port - ds + 1 if ref.device_count is None else ref.device_count
    if ds + dc - 1 > per_port:
        return None, (f"{where}.device_count: {dc} devices from device_start {ds} need {ds + dc - 1}, "
                      f"'{node.name}' has {per_port} per port")
    return {"arg1": node.href, "arg2": ps, "arg3": pc, "arg4": ds, "arg5": dc}, None


def _resolve_side(index: ConfigIndex, refs: list[EndpointRef], where: str,
                  problems: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    whole, subsets = [], []
    for j, r in enumerate(refs):
        node, href, err = _endpoint_node(index, r, f"{where}[{j}]")
        if err is None and r.is_subset:
            sub, err = _subset(index, node, r, f"{where}[{j}]")
            if sub:
                subsets.append(sub)
        elif href:
            whole.append(href)
        if err:
            problems.append(err)
    return whole, subsets


def resolve_endpoint_sets(index: ConfigIndex, cfg: TrafficItemConfig) -> list[dict[str, Any]]:
    resolved, problems = [], []
    for i, es in enumerate(cfg.endpoint_sets):
        where = f"{cfg.name}.endpoint_sets[{i}]"
        srcs, scalable_srcs = _resolve_side(index, es.sources, f"{where}.sources", problems)
        dsts, scalable_dsts = _resolve_side(index, es.destinations, f"{where}.destinations", problems)
        entry: dict[str, Any] = {"Sources": srcs}
        if es.multicast_destinations:
            v6 = ":" in es.multicast_destinations[0].group
            entry["MulticastDestinations"] = [
                {"arg1": False, "arg2": "none", "arg3": d.group, "arg4": d.step or ("::" if v6 else "0.0.0.0"),
                 "arg5": d.count} for d in es.multicast_destinations]
        else:
            entry["Destinations"] = dsts
        if scalable_srcs:
            entry["ScalableSources"] = scalable_srcs
        if scalable_dsts:
            entry["ScalableDestinations"] = scalable_dsts
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


def _is_fcs(s: Any) -> bool:
    dn = (s.DisplayName or "").lower()
    return "frame check sequence" in dn or "fcs" in dn


def _stack_type(s: Any) -> str:
    return (s.StackTypeId or "").lower()


def header_labels(stacks: list[Any]) -> list[str]:
    """'<stackTypeId>#<n>' for each header in packet order (n = occurrence of that type)."""
    seen: dict[str, int] = {}
    out = []
    for s in stacks:
        t = s.StackTypeId or ""
        seen[t] = seen.get(t, 0) + 1
        out.append(f"{t}#{seen[t]}")
    return out


def find_header(stacks: list[Any], ref: str) -> int | None:
    """Index of 'mpls#2', 'mpls' (= mpls#1), a display name or a 1-based position ('4') in stacks."""
    r = ref.strip()
    if r.isdigit():
        i = int(r) - 1
        return i if 0 <= i < len(stacks) else None
    name, _, occ = r.partition("#")
    if occ and not occ.strip().isdigit():
        return None
    n, name = (int(occ) if occ else 1), name.strip().lower()
    hits = [i for i, s in enumerate(stacks) if _stack_type(s) == name] or \
           [i for i, s in enumerate(stacks) if (s.DisplayName or "").strip().lower() == name]
    return hits[n - 1] if 0 < n <= len(hits) else None


def _match_field(fields: list[Any], name: str) -> Any | None:
    want = name.lower()
    return next((x for x in fields if (x.DisplayName or "").lower() == want), None) or \
        next((x for x in fields if (x.FieldTypeId or "").lower() == want), None) or \
        next((x for x in fields if (x.FieldTypeId or "").lower().endswith("." + want)), None)


def _set_field(f: Any, fcfg: PacketHeaderField) -> None:
    if fcfg.auto:
        f.Auto = True
        return
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


def _apply_stack_fields(stack_obj: Any, header, where: str, problems: list[str]) -> None:
    fields = list(stack_obj.Field.find())
    for fcfg in header.fields:
        f = _match_field(fields, fcfg.field_name)
        if f is None:
            problems.append(f"{where}.fields: no field '{fcfg.field_name}' in {stack_obj.DisplayName} "
                            f"(e.g. {_close(fcfg.field_name, [x.DisplayName for x in fields])})")
            continue
        _set_field(f, fcfg)


def _place_headers(ce: Any, wanted: list[tuple[Any, dict[str, Any]]]) -> None:
    """Put the stack entries into the packet in order: the n-th entry of a protocol is the n-th header of
    that protocol. Existing headers (base layers) are matched; the rest are inserted at their natural spot
    (ethernet/vlan/mpls after the last of those, anything else before the FCS), but never before the previous
    entry's header. Positions are re-read after every insert (stack ids shift)."""
    cursor: int | None = None
    seen: dict[str, int] = {}
    for _, tpl in wanted:
        t = tpl["type"].lower()
        seen[t] = seen.get(t, 0) + 1
        stacks = list(ce.Stack.find())
        hits = [i for i, s in enumerate(stacks) if _stack_type(s) == t]
        if len(hits) >= seen[t]:
            cursor = hits[seen[t] - 1] if cursor is None else max(cursor, hits[seen[t] - 1])
            continue
        l2 = [i for i, s in enumerate(stacks) if _stack_type(s) in _L2_TYPES]
        body = [i for i, s in enumerate(stacks) if not _is_fcs(s)] or [len(stacks) - 1]
        spot = l2[-1] if t in _L2_TYPES and l2 else body[-1]
        cursor = spot if cursor is None else max(cursor, spot)
        stacks[cursor].Append(Arg2=tpl["href"])
        cursor += 1


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
    _place_headers(ce, wanted)
    stacks = list(ce.Stack.find())
    seen: dict[str, int] = {}
    for i, (header, tpl) in enumerate(wanted):
        seen[tpl["type"]] = seen.get(tpl["type"], 0) + 1
        idx = find_header(stacks, f"{tpl['type']}#{seen[tpl['type']]}")
        if idx is None:
            problems.append(f"{where}.stack[{i}]: '{header.protocol}' is not in the packet after adding it")
            continue
        _apply_stack_fields(stacks[idx], header, f"{where}.stack[{i}]", problems)


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
    except Exception as exc:
        try:
            ti.remove()
        except Exception as cleanup_exc:  # noqa: BLE001
            log.warning("could not remove half-built traffic item %s: %s", cfg.name, cleanup_exc)
        _raise_self_destined_hint(exc, cfg)
        raise
    return _outcome(ti, warnings, "created")


def _raise_self_destined_hint(exc: Exception, cfg: TrafficItemConfig) -> None:
    """IxNetwork refuses endpoint sets whose source and destination share a port unless the item
    allows self-destined traffic; say which key fixes it."""
    if cfg.self_destined or "not self destined" not in str(exc):
        return
    err = map_exception(exc)
    raise IxToolError(err.code, err.message, "A source and a destination share a port: set self_destined: true.",
                      **err.extra) from exc


def update(ixn: Any, cfg: TrafficItemConfig, ti: Any, index: ConfigIndex, templates: Templates) -> dict[str, Any]:
    resolved = resolve_endpoint_sets(index, cfg)
    ti.update(TrafficType=cfg.traffic_type.value, BiDirectional=cfg.bi_directional, SrcDestMesh=cfg.src_dest_mesh,
              AllowSelfDestined=cfg.self_destined, Enabled=cfg.enabled)
    try:
        _set_endpoint_sets(ti, resolved)
    except Exception as exc:
        _raise_self_destined_hint(exc, cfg)
        raise
    errors, warnings = _finish(ti, cfg, templates)
    if errors:
        raise TrafficProblems(errors)
    return _outcome(ti, warnings, "updated")


def _same_value(a: Any, b: Any) -> bool:
    """'0' == '0x0' == 0 for numeric field values; plain string comparison otherwise."""
    try:
        return int(str(a), 0) == int(str(b), 0)
    except ValueError:
        return str(a) == str(b)


def _field_value(f: Any) -> dict[str, Any]:
    fe: dict[str, Any] = {"field_name": f.DisplayName, "value_type": f.ValueType}
    if f.ValueType == "singleValue":
        fe["single_value"] = f.SingleValue
    elif f.ValueType in ("increment", "decrement"):
        fe.update(start_value=f.StartValue, step_value=f.StepValue, count=f.CountValue)
    elif f.ValueType == "valueList":
        fe["value_list"] = f.ValueList
    return fe


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


# ---------------------------------------------------------------------------- in-place header edits

def config_element(ti: Any, n: int) -> Any:
    from ..errors import NOT_FOUND, IxToolError
    ces = list(ti.ConfigElement.find())
    if not 1 <= n <= len(ces):
        raise IxToolError(NOT_FOUND, f"'{ti.Name}' has {len(ces)} config element(s); config_element is 1-based")
    return ces[n - 1]


def _template_fields(ixn: Any, tpl: dict[str, Any]) -> list[Any]:
    from types import SimpleNamespace
    rows = rest.get(ixn, tpl["href"].split("/ixnetwork/", 1)[1] +
                    "/field?includes=displayName,fieldTypeId,availableValueTypes,readOnly")
    return [SimpleNamespace(DisplayName=r.get("displayName"), FieldTypeId=r.get("fieldTypeId"),
                            AvailableValueTypes=r.get("availableValueTypes"), ReadOnly=r.get("readOnly"))
            for r in rows]


def _check_fields(fields: list[Any], changes: list[PacketHeaderField], header: str, where: str,
                  problems: list[str]) -> None:
    for j, fc in enumerate(changes):
        w = f"{where}.fields[{j}]"
        f = _match_field(fields, fc.field_name)
        if f is None:
            problems.append(f"{w}: no field '{fc.field_name}' in {header} "
                            f"(e.g. {_close(fc.field_name, [x.DisplayName for x in fields])})")
            continue
        if f.ReadOnly:
            problems.append(f"{w}: '{f.DisplayName}' in {header} is read-only")
            continue
        if fc.auto:
            continue
        types = list(f.AvailableValueTypes or [])
        if types and fc.value_type not in types:
            problems.append(f"{w}: '{f.DisplayName}' takes {types}, not '{fc.value_type}'")
        need = {"singleValue": ("single_value", fc.single_value is None),
                "valueList": ("value_list", not fc.value_list),
                "increment": ("start_value/step_value/count", all(v is None for v in (fc.start_value, fc.step_value,
                                                                                       fc.count)))}
        need["decrement"] = need["increment"]
        key, missing = need.get(fc.value_type, ("", False))
        if missing:
            problems.append(f"{w}: {fc.value_type} for '{f.DisplayName}' needs {key}")


def check_header_edits(ixn: Any, ce: Any, changes: list[HeaderChange],
                       templates: Templates) -> tuple[list[str], list[str]]:
    """Validate every change against a simulated packet -> (header labels afterwards, problems)."""
    from types import SimpleNamespace
    sim = [SimpleNamespace(StackTypeId=s.StackTypeId, DisplayName=s.DisplayName, stack=s, tpl=None)
           for s in ce.Stack.find()]
    problems: list[str] = []

    def locate(ref: str, w: str) -> int | None:
        idx = find_header(sim, ref)
        if idx is None:
            problems.append(f"{w}: no header '{ref}' (headers: {header_labels(sim)})")
        return idx

    def fields_of(h: Any) -> list[Any]:
        return list(h.stack.Field.find()) if h.stack is not None else _template_fields(ixn, h.tpl)

    for i, c in enumerate(changes):
        w = f"changes[{i}]"
        if c.header is not None:
            idx = locate(c.header, w)
            if idx is not None:
                _check_fields(fields_of(sim[idx]), c.fields, header_labels(sim)[idx], w, problems)
        elif c.add is not None:
            tpl, cands = templates.find(c.add)
            if tpl is None:
                problems.append(f"{w}: unknown protocol '{c.add}' (e.g. {cands})")
            idx = locate(c.after or c.before, w)
            if tpl is None or idx is None:
                continue
            new = SimpleNamespace(StackTypeId=tpl["type"], DisplayName=tpl["display"], stack=None, tpl=tpl)
            pos = idx + 1 if c.after is not None else idx
            sim.insert(pos, new)
            if c.fields:
                _check_fields(fields_of(new), c.fields, header_labels(sim)[pos], w, problems)
        else:
            idx = locate(c.remove, w)
            if idx is not None and _is_fcs(sim[idx]):
                problems.append(f"{w}: the frame check sequence can't be removed")
            elif idx is not None:
                del sim[idx]
    return header_labels(sim), problems


_FIELD_ATTRS = ("ValueType", "SingleValue", "StartValue", "StepValue", "CountValue", "ValueList")


def _restore_fields(ce: Any, restore: list[list[Any]]) -> None:
    for idx, name, before in reversed(restore):
        try:
            f = _match_field(list(list(ce.Stack.find())[idx].Field.find()), name)
            f.Auto = False
            for attr in _FIELD_ATTRS:
                if before.get(attr) is not None:
                    setattr(f, attr, before[attr])
            if before.get("Auto"):
                f.Auto = True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not restore packet field %s of header %d: %s", name, idx + 1, exc)


def edit_headers(ce: Any, ti: Any, changes: list[HeaderChange], templates: Templates) -> dict[str, Any]:
    """Apply validated changes in order (stack ids shift after every add/remove, so re-read each time), then
    Generate. Field edits are put back if a step raises; headers added or removed before that stay."""
    restore: list[list[Any]] = []  # [stack index, field name, previous values]
    try:
        for c in changes:
            stacks = list(ce.Stack.find())
            if c.header is not None:
                idx = find_header(stacks, c.header)
                fields = list(stacks[idx].Field.find())
                for fc in c.fields:
                    f = _match_field(fields, fc.field_name)
                    restore.append([idx, f.DisplayName, {a: getattr(f, a) for a in ("Auto",) + _FIELD_ATTRS}])
                    _set_field(f, fc)
            elif c.add is not None:
                tpl, _ = templates.find(c.add)
                idx = find_header(stacks, c.after or c.before)
                if c.after is not None:
                    stacks[idx].Append(Arg2=tpl["href"])
                    idx += 1
                else:
                    stacks[idx].Insert(Arg2=tpl["href"])
                for r in restore:
                    r[0] += r[0] >= idx
                fields = list(list(ce.Stack.find())[idx].Field.find())
                for fc in c.fields:
                    _set_field(_match_field(fields, fc.field_name), fc)
            else:
                idx = find_header(stacks, c.remove)
                stacks[idx].Remove()
                restore = [[r[0] - (r[0] > idx), r[1], r[2]] for r in restore if r[0] != idx]
    except Exception:
        _restore_fields(ce, restore)
        raise
    ti.Generate()
    ti.refresh()
    errors = list(ti.Errors or [])
    if errors:
        raise TrafficProblems([f"{ti.Name}: {e}" for e in errors])
    out = _outcome(ti, list(ti.Warnings or []), "edited")
    out["headers"] = header_labels(list(ce.Stack.find()))
    return out


# ---------------------------------------------------------------------------- read

def summarize(ixn: Any, index: ConfigIndex, names: list[str] | None) -> list[dict[str, Any]]:
    root = rest.root(ixn)
    props = ["name", "trafficType", "state", "enabled", "biDirectional", "srcDestMesh", "errors", "warnings",
             "sources", "destinations", "scalableSources", "scalableDestinations", "multicastDestinations", "type",
             "rate", "fixedSize", "trackBy"]
    r = rest.select(ixn, f"{root}/traffic", properties=[], children=[rest.child(
        "^(trafficItem|endpointSet|configElement|frameRate|frameSize|transmissionControl|tracking)$", props)])
    name_of = {n.href.rstrip("/"): n.name for n in index.nodes}

    def label(h: str) -> str:
        h = h.rstrip("/")
        if h.endswith("/protocols"):
            return f"vport:{name_of.get(h[:-len('/protocols')], h)}"
        return name_of.get(h, h)

    def labels(es: dict[str, Any], side: str) -> list[str]:
        scalable = es.get("scalable" + side.capitalize()) or []
        return [label(h) for h in es.get(side) or []] + [
            f"{label(s.get('arg1') or '')} [ports {s.get('arg2')}-{s.get('arg2', 0) + s.get('arg3', 0) - 1}, "
            f"devices {s.get('arg4')}-{s.get('arg4', 0) + s.get('arg5', 0) - 1}]" for s in scalable]

    out = []
    for ti in r[0].get("trafficItem") or []:
        if names and ti.get("name") not in names:
            continue
        ces = ti.get("configElement") or []
        row = {
            "name": ti.get("name"), "type": ti.get("trafficType"), "state": ti.get("state"),
            "enabled": ti.get("enabled"), "bi_directional": ti.get("biDirectional"), "mesh": ti.get("srcDestMesh"),
            "endpoint_sets": [{"sources": labels(es, "sources"), "destinations": labels(es, "destinations"),
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

    def subset(s: dict[str, Any]) -> dict[str, Any]:
        return {**ref(s.get("arg1") or ""), "port_start": s.get("arg2"), "port_count": s.get("arg3"),
                "device_start": s.get("arg4"), "device_count": s.get("arg5")}

    eps = []
    for es in ti.EndpointSet.find():
        entry: dict[str, Any] = {"sources": [ref(h) for h in es.Sources or []] +
                                 [subset(s) for s in es.ScalableSources or []]}
        if es.MulticastDestinations:
            entry["multicast_destinations"] = [{"group": m.get("arg3"), "step": m.get("arg4"), "count": m.get("arg5")}
                                               for m in es.MulticastDestinations]
        else:
            entry["destinations"] = [ref(h) for h in es.Destinations or []] + \
                [subset(s) for s in es.ScalableDestinations or []]
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
        for s in ce.Stack.find():  # every header but the FCS, in packet order, so order and repeats round-trip
            if _is_fcs(s):
                continue
            # only what someone configured: non-auto fields that differ from their default
            fields = [f for f in s.Field.find() if f.Auto is False and not f.ReadOnly and not
                      (f.ValueType == "singleValue" and _same_value(f.SingleValue, f.DefaultValue))]
            entry: dict[str, Any] = {"protocol": s.StackTypeId}
            if fields:
                entry["fields"] = [_field_value(f) for f in fields]
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


def item_packet(ti: Any, n: int, query: str | None) -> dict[str, Any]:
    """The headers of one config element in packet order with every field's current value."""
    ce = config_element(ti, n)
    stacks = list(ce.Stack.find())
    q = (query or "").lower()
    headers = []
    for label, s in zip(header_labels(stacks), stacks):
        if q and q not in label.lower() and q not in (s.DisplayName or "").lower():
            continue
        fields = []
        for f in s.Field.find():
            fe = _field_value(f)
            if f.Auto:
                fe["auto"] = True
            if f.ReadOnly:
                fe["read_only"] = True
            fields.append(fe)
        headers.append({"header": label, "name": (s.DisplayName or "").strip(), "fields": fields})
    return {"traffic_item": ti.Name, "config_element": n, "config_elements": len(list(ti.ConfigElement.find())),
            "headers": headers}
