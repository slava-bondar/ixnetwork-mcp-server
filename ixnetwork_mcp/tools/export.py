"""export_config: scoped ResourceManager.ExportConfig in the ImportConfig xpath dialect."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..errors import VALIDATION, IxToolError
from ..session import SessionManager
from .common import READ_ONLY, ok

TRAFFIC_ITEM_DEFS = [
    "/traffic/trafficItem",
    "/traffic/trafficItem/endpointSet",
    "/traffic/trafficItem/configElement/descendant-or-self::*",
    "/traffic/trafficItem/tracking/descendant-or-self::*",
]
SCOPES: dict[str, list[str]] = {
    "ports": ["/vport/descendant-or-self::*"],
    "locations": ["/locations/descendant-or-self::*"],
    "topologies": ["/topology/descendant-or-self::*"],
    "traffic_items": TRAFFIC_ITEM_DEFS,
}
MAX_OUTLINE = 300


def scope_xpaths(ref: str) -> list[str]:
    ref = ref.strip()
    if ref in SCOPES:
        return SCOPES[ref]
    if not ref.startswith("/") or ref == "/":
        raise IxToolError(VALIDATION, f"unsupported ref '{ref}'",
                          hint="Use an xpath such as '/topology[1]' or '/topology[1]/deviceGroup[2]', or one of: "
                               + ", ".join(SCOPES) + ". Whole-config export is not allowed (can be >100 MB).")
    return [ref if "descendant" in ref else f"{ref}/descendant-or-self::*"]


def outline(doc: Any, acc: list[str]) -> None:
    if isinstance(doc, dict):
        xp = doc.get("xpath")
        if isinstance(xp, str) and "/multivalue[" not in xp and xp != "/":
            acc.append(xp)
        for v in doc.values():
            if isinstance(v, (dict, list)):
                outline(v, acc)
    elif isinstance(doc, list):
        for v in doc:
            outline(v, acc)


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def export_config(
        ref: str,
        target: str | None = None,
        include_defaults: bool = False,
        max_kb: int = 256,
    ) -> dict[str, Any]:
        """Export part of the running configuration in IxNetwork's ImportConfig JSON dialect
        (nodes addressed by positional xpath, multivalues as
        /multivalue[@source = '<xpath> <attr>']/singleValue|counter|valueList...).
        `ref` is an xpath like '/topology[1]' or '/topology[1]/deviceGroup[2]', or one of
        'ports', 'locations', 'topologies', 'traffic_items'. Exports are slow (seconds per topology);
        prefer get_session_overview for summaries. If the result exceeds `max_kb`, an outline of node
        xpaths is returned instead so you can export a narrower ref. Useful to learn how a
        configuration built in the GUI is expressed."""
        xpaths = scope_xpaths(ref)
        ts = manager.resolve(target)
        doc = await manager.call(target, lambda ixn: rest.export(ixn, xpaths, include_defaults))
        size_kb = round(len(json.dumps(doc, separators=(",", ":"))) / 1024, 1)
        if size_kb <= max_kb:
            return ok(ts.target.name, {"xpaths": xpaths, "size_kb": size_kb, "config": doc})
        nodes: list[str] = []
        outline(doc, nodes)
        return ok(ts.target.name, {
            "xpaths": xpaths, "size_kb": size_kb, "max_kb": max_kb,
            "outline": nodes[:MAX_OUTLINE], "outline_total": len(nodes),
        }, truncated=True, next_steps=["export_config with a narrower ref taken from the outline, or raise max_kb"])
