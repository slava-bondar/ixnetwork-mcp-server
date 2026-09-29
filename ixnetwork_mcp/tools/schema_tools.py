"""Schema tools: search_node_types, describe_node_type (+ schema_for helper)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP

from .. import rest
from ..errors import NOT_FOUND, IxToolError
from ..refs import ConfigIndex
from ..schema.crawl import crawl_to_file
from ..schema.index import SchemaIndex, get_index, user_cache_path
from ..session import SessionManager
from ..values import mv_to_value
from .common import READ_ONLY, ok

log = logging.getLogger("ixnetwork_mcp.schema")
_crawl_locks: dict[str, threading.Lock] = {}
_crawl_guard = threading.Lock()


def _crawl_build(base: str, headers: dict[str, str], verify: bool, build: str) -> None:
    with _crawl_guard:
        lock = _crawl_locks.setdefault(build, threading.Lock())
    with lock:  # two targets on the same unknown build: crawl once
        idx, exact = get_index(build)
        if exact:
            return
        log.info("no schema cache for build %s; crawling %s", build, base)
        crawl_to_file(base, build, user_cache_path(build), headers=headers, verify=verify)
        get_index.cache_clear()


async def schema_for(manager: SessionManager, target: str | None) -> tuple[SchemaIndex, list[str]]:
    """Schema index matching the target's IxNetwork build. Crawls an unknown build once
    (~20 s) into ~/.ixnetwork-mcp/schema-cache; if the target is unreachable, falls back to
    the nearest cached build with a warning."""
    warnings: list[str] = []
    ts = None
    if manager.sessions:
        ts = manager.resolve(target)
        if ts.build is None:
            try:
                await manager.call(target, lambda ixn: None)
            except IxToolError as exc:
                warnings.append(f"target '{ts.target.name}' not reachable ({exc.code}); using cached schema")
    build = ts.build if ts else None
    idx, exact = get_index(build)
    if build and not exact and ts is not None and ts.rest_base:
        try:
            await anyio.to_thread.run_sync(_crawl_build, ts.rest_base, ts.rest_headers, ts.target.verify_cert, build)
            idx, exact = get_index(build)
        except Exception as exc:  # crawl failures must not break read-only schema lookups
            warnings.append(f"schema crawl for build {build} failed: {exc}")
    if build and not exact:
        warnings.append(f"no schema for IxNetwork build {build}; showing build {idx.build}")
    if idx.mv_build and idx.mv_build != idx.build:
        warnings.append(f"multivalue formats/enums are from build {idx.mv_build}; "
                        "use describe_node_type(live_ref=...) for exact values")
    return idx, warnings


def live_multivalues(ixn: Any, ref: str) -> dict[str, Any]:
    node = ConfigIndex.build(ixn, props=["name"]).resolve(ref)
    r = rest.select(ixn, node.href, properties=["*"], inlines=rest.MV_INLINES)[0]
    out: dict[str, Any] = {"ref": {"name": node.name, "type": node.type, "xpath": node.xpath}, "attributes": {}}
    for k, v in r.items():
        if rest.is_multivalue(v):
            row = {"format": v.get("format"), "value": mv_to_value(v)}
            if v.get("enums"):
                row["enums"] = v["enums"]
            out["attributes"][k] = row
    out["type_path"] = node.type_path
    return out


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def search_node_types(query: str, under: str | None = None, limit: int = 20,
                                target: str | None = None) -> dict[str, Any]:
        """Search the IxNetwork object model (all NGPF node types, traffic, ports) by keywords,
        e.g. 'dhcp server pool', 'igmp group', 'bgp route'. Returns type names, a typical path
        (where the type lives in the hierarchy), whether it can be created, and descriptions.
        `under` restricts to a subtree type path such as 'topology/deviceGroup/ethernet/ipv4'.
        Works offline from the cached schema of the target's IxNetwork build."""
        idx, warnings = await schema_for(manager, target)
        return ok(target, {"schema_build": idx.build, "matches": idx.search(query, under, max(1, min(limit, 50)))},
                  warnings=warnings)

    @app.tool(annotations=READ_ONLY)
    async def describe_node_type(
        node_type: str,
        attributes: list[str] | None = None,
        include: list[Literal["attributes", "children", "operations"]] | None = None,
        include_readonly: bool = False,
        live_ref: str | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Describe an IxNetwork node type: where it lives, whether it can be created, its
        attributes (kind 'multivalue' = per-device value pattern, 'settable' = plain value,
        'readonly'), formats, enum values and defaults, its child types and (on request) its
        operations. `node_type` is a type name ('bgpIpv4Peer') or a type path
        ('topology/deviceGroup/ethernet/ipv4/bgpIpv4Peer'). `attributes` filters by name
        substrings (e.g. ['as', 'hold']). `live_ref` (name/xpath of an existing object of this
        type) adds the object's current multivalue patterns and exact live enums."""
        idx, warnings = await schema_for(manager, target)
        desc = idx.describe(node_type, attributes=attributes, include=list(include) if include else None,
                            include_readonly=include_readonly)
        if desc is None:
            raise IxToolError(NOT_FOUND, f"unknown node type '{node_type}'",
                              candidates=[m["type"] for m in idx.search(node_type, limit=8)] or None,
                              hint="Use search_node_types to find the right type name.")
        if live_ref:
            desc["live"] = await manager.call(target, lambda ixn: live_multivalues(ixn, live_ref))
        return ok(target, desc, warnings=warnings)
