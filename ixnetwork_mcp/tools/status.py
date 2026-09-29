"""get_protocol_status: per-protocol session state across the configuration."""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..refs import ConfigIndex
from ..session import SessionManager
from .common import READ_ONLY, ok
from .discovery import STACK_REGEX

PROBLEM_STATUS = {"error", "mixed"}


def ranges(indices: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3,5,7-8' (1-based device indices)."""
    out: list[str] = []
    start = prev = None
    for i in indices:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = i
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def _group(per_device: list[Any], skip: set[str]) -> dict[str, str]:
    groups: dict[str, list[int]] = {}
    for i, v in enumerate(per_device or [], start=1):
        key = str(v)
        if key not in skip:
            groups.setdefault(key, []).append(i)
    return {k: ranges(v) for k, v in groups.items()}


def status(ixn: Any, scope: str | None, per_device: bool, only_problems: bool) -> dict[str, Any]:
    props = ["name", "status", "stateCounts", "errors", "count"]
    if per_device:
        props += ["sessionStatus", "sessionInfo"]
    root = rest.root(ixn)
    if scope:
        hrefs = [ConfigIndex.build(ixn, props=["name"]).resolve(scope).href]
    else:
        hrefs = [f"{root}/topology/{t['id']}" for t in rest.get(ixn, "topology?includes=name")]
    rows: list[dict[str, Any]] = []
    summary: dict[str, dict[str, int]] = {}

    def walk(node: dict[str, Any]) -> None:
        sc = rest.state_counts(node.get("stateCounts"))
        if sc and sc["total"]:
            ntype = rest.node_type(node["xpath"])
            row: dict[str, Any] = {"name": node.get("name"), "type": ntype, "xpath": node["xpath"],
                                   "status": node.get("status"), "sessions": sc}
            errs = node.get("errors")
            if errs:
                row["errors"] = json.dumps(errs, default=str)[:600]
            if per_device:
                row["devices_by_state"] = _group(node.get("sessionStatus") or [], set())
                info = _group(node.get("sessionInfo") or [], {"none", "None", ""})
                if info:
                    row["devices_by_info"] = info
            agg = summary.setdefault(ntype, {"up": 0, "down": 0, "not_started": 0, "total": 0})
            for k in agg:
                agg[k] += sc[k]
            problem = sc["down"] > 0 or bool(errs) or node.get("status") in PROBLEM_STATUS
            if problem:
                row["problem"] = True
            if problem or not only_problems:
                rows.append(row)
        for kid in rest.child_nodes(node):
            walk(kid)

    for href in hrefs:
        r = rest.select(ixn, href, properties=props, children=[rest.child(STACK_REGEX, props)])
        walk(r[0])
    return {"summary_by_type": summary, "nodes": rows}


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def get_protocol_status(
        scope: str | None = None,
        detail: Literal["summary", "per_device"] = "summary",
        only_problems: bool = False,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Protocol/session state for every interface and protocol object (ethernet, ipv4/ipv6,
        BGP, OSPF, IS-IS, IGMP/MLD, DHCP ...): status and up/down/not-started session counts, errors,
        plus totals per protocol type. `scope` limits to a subtree (ref of a topology, device group
        or protocol object). `detail='per_device'` adds which device indices are up/down (e.g.
        {'up': '1-8', 'down': '9-10'}) and per-device failure reasons. `only_problems` returns only
        objects with down sessions, errors or error/mixed status."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: status(ixn, scope, detail == "per_device", only_problems)))
