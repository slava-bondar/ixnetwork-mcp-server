"""The agent-facing *Value* language for multivalue attributes.

Readback renders a server multivalue (select with rest.MV_INLINES) in the same shape the
agent writes, so values can be copied back unchanged:

    "10.0.0.1"                                   single value
    {"start": "10.0.0.1", "step": "0.0.0.1", "direction": "increment"}   counter
    {"list": ["a", "b"]}                         value list
    {"string": "Router-{Inc:1,1}"}               string pattern
    {"random_range": {"min": .., "max": .., "step": .., "seed": ..}}
    {"alternate": v} | {"random": true}
    {"custom": {"start": s, "step": t, "increment": [{"count": n, "value": v}, ...]}}
        custom: s, s+v, ... (n values per increment, increments may nest), then s+t, ...

Optional keys on the dict forms (a single value becomes {"single": v, ...} when needed):
    "port_step": {"enabled": bool, "step": s}  nest owned by the topology ("Port Step")
    "nest": [{"owner": name, "enabled": bool, "step": s}, ...]  other nest levels (innermost first)
    "overlays": [{"index": i, "value": v, "count": n}]
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

_DETAIL_SKIP = {"href", "xpath", "id", "links"}


def _detail(mv: dict[str, Any], key: str) -> dict[str, Any]:
    d = mv.get(key)
    return {k: v for k, v in d.items() if k not in _DETAIL_SKIP} if isinstance(d, dict) else {}


def mv_to_value(mv: dict[str, Any], *, show_disabled_nest: bool = False) -> Any:
    """Render a multivalue from a select-with-inlines response as a Value."""
    pattern = mv.get("pattern")
    d = _detail(mv, pattern) if pattern else {}
    single = False
    if pattern == "singleValue":
        value: Any = d.get("value")
        single = True
    elif pattern == "counter":
        value = {"start": d.get("start"), "step": d.get("step"), "direction": d.get("direction", "increment")}
    elif pattern == "valueList":
        value = {"list": d.get("values", [])}
    elif pattern == "string":
        value = {"string": d.get("pattern")}
    elif pattern == "repeatableRandomRange":
        value = {"random_range": {k: d.get(k) for k in ("min", "max", "step", "seed") if k in d}}
    elif pattern == "alternate":
        value = {"alternate": d.get("value")}
    elif pattern == "random":
        value = {"random": True}
    elif pattern == "custom":
        value = {"custom": _custom_readback(d)}
    else:
        value = {"pattern": pattern, **d}

    extras: dict[str, Any] = {}
    other_nests = []
    for n in mv.get("nest") or []:
        entry = {"enabled": n.get("enabled"), "step": n.get("step")}
        if n.get("description") == "Port Step":
            if n.get("enabled") or show_disabled_nest:
                extras["port_step"] = entry
        elif n.get("enabled") or show_disabled_nest:
            other_nests.append({"owner": n.get("description"), **entry})
    if other_nests:
        extras["nest"] = other_nests
    overlays = [
        {"index": o.get("index"), "value": o.get("value"), "count": o.get("count", 1)}
        for o in (mv.get("overlay") or []) if isinstance(o, dict)
    ]
    if overlays:
        extras["overlays"] = overlays
    if not extras:
        return value
    if single:
        return {"single": value, **extras}
    return {**value, **extras}


def _custom_readback(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: d[k] for k in ("start", "step") if k in d}
    incs = [_custom_increment(i) for i in d.get("increment") or [] if isinstance(i, dict)]
    if incs:
        out["increment"] = incs
    return out


def _custom_increment(i: dict[str, Any]) -> dict[str, Any]:
    out = {k: i[k] for k in ("count", "value") if k in i}
    kids = [_custom_increment(k) for k in i.get("increment") or [] if isinstance(k, dict)]
    if kids:
        out["increment"] = kids
    return out


# ---------------------------------------------------------------------------- write side

PATTERN_KEYS = {"single", "start", "step", "direction", "list", "string", "random_range", "alternate", "random",
                "custom"}
EXTRA_KEYS = {"port_step", "nest", "overlays"}
_MAC = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")
_HEX = re.compile(r"^(0x)?[0-9a-fA-F]*$")


class ValueSyntaxError(ValueError):
    pass


@dataclass
class ValueSpec:
    pattern: str | None  # singleValue|counter|valueList|string|repeatableRandomRange|alternate|random|None
    fields: dict[str, Any] = field(default_factory=dict)
    port_step: dict[str, Any] | None = None
    nest: list[dict[str, Any]] | None = None
    overlays: list[dict[str, Any]] | None = None


def _scalar(v: Any, where: str) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, str)):
        return str(v)
    raise ValueSyntaxError(f"{where}: expected a scalar (string/number/bool), got {type(v).__name__}")


def _step_entry(v: Any, where: str) -> dict[str, Any]:
    if v is False:
        return {"enabled": False}
    if isinstance(v, dict):
        unknown = set(v) - {"enabled", "step"}
        if unknown:
            raise ValueSyntaxError(f"{where}: unknown keys {sorted(unknown)} (expected enabled, step)")
        out = {"enabled": bool(v.get("enabled", True))}
        if "step" in v:
            out["step"] = _scalar(v["step"], where + ".step")
        return out
    return {"enabled": True, "step": _scalar(v, where)}


def parse_value(raw: Any, attr: str = "value") -> ValueSpec:
    """Normalize an agent-supplied Value (see module docstring)."""
    if isinstance(raw, list):
        raw = {"list": raw}
    if not isinstance(raw, dict):
        return ValueSpec("singleValue", {"value": _scalar(raw, attr)})
    unknown = set(raw) - PATTERN_KEYS - EXTRA_KEYS
    if unknown:
        raise ValueSyntaxError(
            f"{attr}: unknown keys {sorted(unknown)}; use a plain value, {{start, step, direction}}, {{list}}, "
            "{string}, {random_range}, {alternate}, {custom}, {single}, plus optional port_step / nest / overlays")
    if "single" in raw:
        spec = ValueSpec("singleValue", {"value": _scalar(raw["single"], attr)})
    elif "start" in raw or "step" in raw:
        if "start" not in raw or "step" not in raw:
            raise ValueSyntaxError(f"{attr}: a counter needs both start and step")
        direction = raw.get("direction", "increment")
        if direction not in ("increment", "decrement"):
            raise ValueSyntaxError(f"{attr}: direction must be increment or decrement")
        spec = ValueSpec("counter", {"start": _scalar(raw["start"], attr + ".start"),
                                     "step": _scalar(raw["step"], attr + ".step"), "direction": direction})
    elif "list" in raw:
        items = raw["list"]
        if not isinstance(items, list) or not items:
            raise ValueSyntaxError(f"{attr}: list must be a non-empty array")
        spec = ValueSpec("valueList", {"values": [_scalar(v, f"{attr}.list[{i}]") for i, v in enumerate(items)]})
    elif "string" in raw:
        spec = ValueSpec("string", {"pattern": _scalar(raw["string"], attr + ".string")})
    elif "random_range" in raw:
        rr = raw["random_range"]
        if not isinstance(rr, dict) or not {"min", "max"} <= set(rr):
            raise ValueSyntaxError(f"{attr}: random_range needs min and max (optional step, seed)")
        spec = ValueSpec("repeatableRandomRange", {k: _scalar(rr[k], f"{attr}.random_range.{k}")
                                                   for k in ("min", "max", "step", "seed") if k in rr})
    elif "alternate" in raw:
        spec = ValueSpec("alternate", {"value": _scalar(raw["alternate"], attr + ".alternate")})
    elif raw.get("random") is True:
        spec = ValueSpec("random", {})
    elif "custom" in raw:
        c = raw["custom"]
        if not isinstance(c, dict) or "start" not in c or set(c) - {"start", "step", "increment"}:
            raise ValueSyntaxError(f"{attr}: custom needs start (optional step, increment: [{{count, value}}])")
        fields: dict[str, Any] = {"start": _scalar(c["start"], attr + ".custom.start"),
                                  "step": _scalar(c.get("step", 0), attr + ".custom.step")}
        if "increment" in c:
            fields["increment"] = _increments(c["increment"], attr + ".custom.increment")
        spec = ValueSpec("custom", fields)
    else:
        spec = ValueSpec(None)  # only port_step / nest / overlays: keep the current pattern
    if "port_step" in raw:
        spec.port_step = _step_entry(raw["port_step"], attr + ".port_step")
    if "nest" in raw:
        if not isinstance(raw["nest"], list):
            raise ValueSyntaxError(f"{attr}: nest must be an array (innermost level first)")
        spec.nest = [_step_entry({k: v for k, v in n.items() if k != "owner"} if isinstance(n, dict) else n,
                                 f"{attr}.nest[{i}]") for i, n in enumerate(raw["nest"])]
    if "overlays" in raw:
        ovs = raw["overlays"]
        if not isinstance(ovs, list):
            raise ValueSyntaxError(f"{attr}: overlays must be an array of {{index, value, count}}")
        spec.overlays = []
        for i, o in enumerate(ovs):
            if not isinstance(o, dict) or "index" not in o or "value" not in o:
                raise ValueSyntaxError(f"{attr}.overlays[{i}]: needs index (1-based device) and value")
            idx, count = int(o["index"]), int(o.get("count", 1))
            if idx < 1 or count < 1:
                raise ValueSyntaxError(f"{attr}.overlays[{i}]: index and count must be >= 1")
            spec.overlays.append({"index": idx, "count": count, "value": _scalar(o["value"], f"{attr}.overlays[{i}]")})
    if spec.pattern is None and not (spec.port_step or spec.nest or spec.overlays):
        raise ValueSyntaxError(f"{attr}: empty value")
    return spec


def _increments(raw: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueSyntaxError(f"{where}: expected a non-empty array of {{count, value}}")
    out = []
    for i, inc in enumerate(raw):
        w = f"{where}[{i}]"
        if not isinstance(inc, dict) or "count" not in inc or "value" not in inc or \
                set(inc) - {"count", "value", "increment"}:
            raise ValueSyntaxError(f"{w}: needs count and value (optional nested increment)")
        try:
            count = int(inc["count"])
        except (TypeError, ValueError):
            raise ValueSyntaxError(f"{w}.count: expected a whole number") from None
        if count < 1:
            raise ValueSyntaxError(f"{w}.count: must be >= 1")
        entry: dict[str, Any] = {"count": count, "value": _scalar(inc["value"], w + ".value")}
        if "increment" in inc:
            entry["increment"] = _increments(inc["increment"], w + ".increment")
        out.append(entry)
    return out


def _increment_values(incs: list[dict[str, Any]], where: str) -> list[tuple[str, str]]:
    out = []
    for i, inc in enumerate(incs):
        out.append((inc["value"], f"{where}[{i}].value"))
        out += _increment_values(inc.get("increment") or [], f"{where}[{i}].increment")
    return out


def _check(fmt: str | None, enums: list[str] | None, v: str, where: str, step: bool = False) -> str | None:
    try:
        if fmt == "ipv4":
            ipaddress.IPv4Address(v)
        elif fmt == "ipv6":
            ipaddress.IPv6Address(v)
        elif fmt == "mac":
            if not _MAC.match(v):
                return f"{where}: '{v}' is not a MAC address (aa:bb:cc:dd:ee:ff)"
        elif fmt == "decimal":
            float(v)
        elif fmt == "bool":
            if v.lower() not in ("true", "false"):
                return f"{where}: '{v}' is not a boolean (true/false)"
        elif fmt == "enum" and enums and not step:
            if v not in enums:
                return f"{where}: '{v}' is not one of {enums}"
        elif fmt == "hex":
            if not _HEX.match(v):
                return f"{where}: '{v}' is not hexadecimal"
    except ValueError:
        return f"{where}: '{v}' is not a valid {fmt} value"
    return None


def validate_value(spec: ValueSpec, attr: str, fmt: str | None, enums: list[str] | None,
                   patterns: list[str] | None) -> list[str]:
    """Check a parsed Value against the attribute's live format / enums / allowed patterns."""
    errs: list[str] = []
    if spec.pattern and patterns and spec.pattern not in patterns:
        errs.append(f"{attr}: pattern {spec.pattern} not allowed here (allowed: {patterns})")
    f = spec.fields
    checks: list[tuple[str, str, bool]] = []
    if spec.pattern in ("singleValue", "alternate"):
        checks.append((f["value"], attr, False))
    elif spec.pattern == "counter":
        if fmt in ("enum", "bool", "string"):
            errs.append(f"{attr}: a counter is not possible for {fmt} values")
        checks += [(f["start"], f"{attr}.start", False), (f["step"], f"{attr}.step", True)]
    elif spec.pattern == "valueList":
        checks += [(v, f"{attr}.list[{i}]", False) for i, v in enumerate(f["values"])]
    elif spec.pattern == "custom":
        if fmt in ("enum", "bool", "string"):
            errs.append(f"{attr}: a custom pattern is not possible for {fmt} values")
        checks += [(f["start"], f"{attr}.custom.start", False), (f["step"], f"{attr}.custom.step", True)]
        checks += [(v, w, True) for v, w in _increment_values(f.get("increment") or [], f"{attr}.custom.increment")]
    elif spec.pattern == "repeatableRandomRange":
        checks += [(f[k], f"{attr}.random_range.{k}", k == "step") for k in ("min", "max", "step") if k in f]
    for o in spec.overlays or []:
        checks.append((o["value"], f"{attr}.overlay@{o['index']}", False))
    for n in ([spec.port_step] if spec.port_step else []) + (spec.nest or []):
        if "step" in n:
            checks.append((n["step"], f"{attr} step", True))
    for v, where, is_step in checks:
        e = _check(fmt, enums, v, where, is_step)
        if e:
            errs.append(e)
    return errs


def mv_xpath(node_xpath: str, attr: str) -> str:
    return f"/multivalue[@source = '{node_xpath} {attr}']"


def compile_value(node_xpath: str, attr: str, spec: ValueSpec, nest_count: int) -> list[dict[str, Any]]:
    """Flat ImportConfig entries for one multivalue. ``nest_count`` = number of nest levels the
    multivalue has (Port Step is the last one)."""
    mv = mv_xpath(node_xpath, attr)
    out: list[dict[str, Any]] = []
    if spec.pattern == "custom":
        out.append({"xpath": f"{mv}/custom", "start": spec.fields["start"], "step": spec.fields["step"]})
        out += _compile_increments(f"{mv}/custom", spec.fields.get("increment") or [])
    elif spec.pattern:
        out.append({"xpath": f"{mv}/{spec.pattern}", **spec.fields})
    if spec.nest:
        if len(spec.nest) > nest_count:
            raise ValueSyntaxError(f"{attr}: {len(spec.nest)} nest levels given, the object has {nest_count}")
        out += [{"xpath": f"{mv}/nest[{i}]", **n} for i, n in enumerate(spec.nest, 1)]
    if spec.port_step:
        if nest_count < 1:
            raise ValueSyntaxError(f"{attr}: this attribute has no port step")
        out.append({"xpath": f"{mv}/nest[{nest_count}]", **spec.port_step})
    if spec.overlays is not None:
        out += [{"xpath": f"{mv}/overlay[{i}]", **o} for i, o in enumerate(spec.overlays, 1)]
    return out


def _compile_increments(parent: str, incs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, inc in enumerate(incs, 1):
        x = f"{parent}/increment[{i}]"
        out.append({"xpath": x, "count": inc["count"], "value": inc["value"]})
        out += _compile_increments(x, inc.get("increment") or [])
    return out


def _canon(fmt: str | None, v: Any) -> Any:
    s = str(v)
    try:
        if fmt == "ipv6":
            return str(ipaddress.IPv6Address(s))
        if fmt == "ipv4":
            return str(ipaddress.IPv4Address(s))
        if fmt == "decimal":
            n = float(s)
            return int(n) if n.is_integer() else n
        if fmt == "hex":  # IxNetwork zero-pads hex values (e.g. IS-IS area 490001)
            return int(s.lower().replace("0x", "").replace(" ", "") or "0", 16)
    except ValueError:
        pass
    return s.lower() if fmt in ("mac", "hex", "bool") else s


def _norm_incs(incs: list[dict[str, Any]], fmt: str | None) -> list[Any]:
    return [(int(i.get("count", 0)), _canon(fmt, i.get("value")), _norm_incs(i.get("increment") or [], fmt))
            for i in incs]


def same_value(spec: ValueSpec, readback: Any, fmt: str | None) -> bool:
    """Does a readback Value (mv_to_value) match what was requested?"""
    rb = readback if isinstance(readback, dict) else {"single": readback}
    if spec.pattern == "singleValue":
        got = rb.get("single") if "single" in rb else (readback if not isinstance(readback, dict) else None)
        if _canon(fmt, got) != _canon(fmt, spec.fields["value"]):
            return False
    elif spec.pattern == "counter":
        if any(_canon(fmt, rb.get(k)) != _canon(fmt, spec.fields[k]) for k in ("start", "step")):
            return False
        if rb.get("direction", "increment") != spec.fields["direction"]:
            return False
    elif spec.pattern == "valueList":
        if [_canon(fmt, x) for x in rb.get("list", [])] != [_canon(fmt, x) for x in spec.fields["values"]]:
            return False
    elif spec.pattern == "custom":
        c = rb.get("custom") or {}
        if any(_canon(fmt, c.get(k)) != _canon(fmt, spec.fields[k]) for k in ("start", "step")):
            return False
        if _norm_incs(c.get("increment") or [], fmt) != _norm_incs(spec.fields.get("increment") or [], fmt):
            return False
    elif spec.pattern is not None:
        pass  # other patterns: trust the import result
    if spec.port_step is not None:
        ps = rb.get("port_step", {"enabled": False})
        if ps.get("enabled", False) != spec.port_step["enabled"]:
            return False
        if spec.port_step["enabled"] and "step" in spec.port_step and \
                _canon(fmt, ps.get("step")) != _canon(fmt, spec.port_step["step"]):
            return False
    return True
