"""Navigation tools: find_nodes, get_node."""

from __future__ import annotations

import copy
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..refs import ConfigIndex
from ..schema.index import SchemaIndex, _compact_default
from ..session import SessionManager
from ..values import mv_to_value
from .common import READ_ONLY, ok
from .schema_tools import schema_for

_SKIP_KEYS = {"href", "id", "xpath", "links"}


def find(ixn: Any, node_type: str | None, under: str | None, name: str | None,
         limit: int, include_auto: bool) -> dict[str, Any]:
    idx = ConfigIndex.build(ixn)
    nodes = idx.nodes
    if under:
        nodes = idx.subtree(idx.resolve(under))
    if node_type:
        t = node_type.strip().strip("/").lower()
        nodes = [n for n in nodes if n.type.lower() == t or n.type_path.lower().endswith(t)]
    elif not include_auto:
        nodes = [n for n in nodes if not n.auto]
    if name:
        nodes = [n for n in nodes if name.lower() in (n.name or "").lower()]
    return {"total": len(nodes), "nodes": [n.brief() for n in nodes[:limit]]}


def _attr_kinds(schema: SchemaIndex, type_path: str) -> tuple[dict[str, str], dict[str, Any], set[str]]:
    """(kind by attr, default by attr, deprecated attrs) from the schema for a type path."""
    info = schema.resolve(type_path)
    kinds: dict[str, str] = {}
    defaults: dict[str, Any] = {}
    deprecated: set[str] = set()
    if info is None:
        return kinds, defaults, deprecated
    mv = schema.mv.get(info.canonical, {})
    for a in info.raw.get("attributes") or []:
        t = a.get("type") or {}
        is_mv = t.get("name") == "href" and any(h.endswith("/multivalue") for h in t.get("hrefs") or [])
        kinds[a["name"]] = "multivalue" if is_mv else ("readonly" if a.get("readOnly") else "settable")
        if a.get("deprecated"):
            deprecated.add(a["name"])
        if is_mv and "default" in mv.get(a["name"], {}):
            defaults[a["name"]] = _compact_default(mv[a["name"]]["default"])
        elif not is_mv and a.get("default") not in (None, "kVoid"):
            defaults[a["name"]] = a["default"]
    return kinds, defaults, deprecated


def _strip_nest(v: Any) -> Any:
    if isinstance(v, dict):
        base = {k: x for k, x in v.items() if k not in ("port_step", "nest", "overlays")}
        return base.get("single") if set(base) == {"single"} else base
    return v


def detail(ixn: Any, ref: str, schema: SchemaIndex, attributes: list[str] | None, children: bool,
           expand_values: int, changed_only: bool) -> dict[str, Any]:
    idx = ConfigIndex.build(ixn, props=["name"])
    node = idx.resolve(ref)
    inlines = copy.deepcopy(rest.MV_INLINES)
    if expand_values > 0:
        inlines[0]["properties"].append("values")
    r = rest.select(ixn, node.href, properties=["*"], inlines=inlines)[0]
    kinds, defaults, deprecated = _attr_kinds(schema, node.type_path)
    wanted = [a.lower() for a in attributes] if attributes else None
    settable: dict[str, Any] = {}
    state: dict[str, Any] = {}
    values: dict[str, list[Any]] = {}
    for key, raw in r.items():
        if key in _SKIP_KEYS:
            continue
        if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "xpath" in raw[0]:
            continue  # child objects
        if wanted is not None:
            if not any(w in key.lower() for w in wanted):
                continue
        elif key in deprecated:
            continue
        if rest.is_multivalue(raw):
            val = mv_to_value(raw)
            if changed_only and key in defaults and _strip_nest(val) == defaults[key]:
                continue
            settable[key] = val
            if expand_values > 0:
                values[key] = (raw.get("values") or [])[:expand_values]
            continue
        if key == "stateCounts":
            raw = rest.state_counts(raw)
        if kinds.get(key) == "readonly":
            if raw not in (None, "", [], {}):
                state[key] = raw
        else:
            if changed_only and ((key in defaults and raw == defaults[key]) or raw in ([], "", None)):
                continue
            settable[key] = raw
    out: dict[str, Any] = {
        "ref": {"name": node.name, "type": node.type, "xpath": node.xpath, "href": node.href},
        "attributes": settable,
        "state": state,
    }
    if values:
        out["values"] = values
    if children:
        out["children"] = [{"name": c.name, "type": c.type, "xpath": c.xpath} for c in idx.children(node)]
    if node.type == "deviceGroup" or "count" in r:
        out["devices"] = r.get("count")
    return out


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def find_nodes(
        node_type: str | None = None,
        under: str | None = None,
        name: str | None = None,
        limit: int = 50,
        include_auto: bool = False,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Find configuration objects (topologies, device groups, protocol stacks, network groups,
        vports, traffic items) in the live session. Filters: `node_type` (e.g. 'bgpIpv4Peer',
        'deviceGroup', 'vport', 'trafficItem'), `under` (ref of an ancestor, e.g. a topology name),
        `name` (case-insensitive substring). Returns name, type, xpath, device count, status and
        session counts. Auto-created helper objects are hidden unless `include_auto` or asked for by
        type. Use the returned name (or xpath) as `ref` in other tools."""
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: find(ixn, node_type, under, name,
                                                             max(1, min(limit, 500)), include_auto))
        return ok(ts.target.name, result, truncated=result["total"] > len(result["nodes"]) or None)

    @app.tool(annotations=READ_ONLY)
    async def get_node(
        ref: str,
        attributes: list[str] | None = None,
        changed_only: bool = False,
        children: bool = True,
        expand_values: int = 0,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Show one configuration object. `ref` is a name ('BGP Peer 1'), 'type:name', an xpath or
        an href. `attributes` holds settable values: per-device multivalues are rendered as Values
        (a plain value, {start, step, direction} counter, {list: [...]}, with port_step/nest when
        enabled) — the same form used when changing them. `state` holds read-only status.
        `attributes=[...]` filters by name substrings; `changed_only` hides values still at their
        default (approximate for auto-numbered values such as router ids); `expand_values=N` adds the first N per-device values of each multivalue;
        `children` lists child objects."""
        ts = manager.resolve(target)
        schema, warnings = await schema_for(manager, target)
        result = await manager.call(target, lambda ixn: detail(ixn, ref, schema, attributes, children,
                                                               max(0, min(expand_values, 1000)), changed_only))
        return ok(ts.target.name, result, warnings=warnings)
