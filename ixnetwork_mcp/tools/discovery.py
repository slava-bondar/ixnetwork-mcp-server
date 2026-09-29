"""Discovery tools: targets, session overview, usage guide."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..errors import NOT_FOUND, IxToolError
from ..session import SessionManager
from .common import READ_ONLY, ok

GUIDE_DIR = Path(__file__).resolve().parent.parent / "guide"

# Stack nodes hidden from the overview: auto-created plumbing and per-protocol sub-lists.
_OVERVIEW_SKIP = (
    r"tag|port|connector|learnedInfo|tlvProfile|vpnParameter|pbbEVpnParameter|routerData|"
    r"bridgeData|iptv|Rfc8277LabelStack|.*Params|dhcp6Iana[0-9]*|dhcp6Iapd[0-9]*|"
    r".*List|.*Info|.*Sessions|.*Profile|.*TrafficEngineering|bgpSRGB.*"
)
STACK_REGEX = rf"^(?!({_OVERVIEW_SKIP})$)[A-Za-z0-9]+$"
_NODE_PROPS = ["name", "multiplier", "count", "status", "stateCounts", "ports", "useVlans", "vlanCount"]

Section = Literal["ports", "topologies", "traffic", "status"]


def _tree(node: dict[str, Any], depth: int, max_depth: int, vport_names: dict[str, str]) -> dict[str, Any]:
    ntype = rest.node_type(node["xpath"])
    out: dict[str, Any] = {"type": ntype, "name": node.get("name")}
    if ntype in ("deviceGroup", "networkGroup") and "multiplier" in node:
        out["multiplier"] = node["multiplier"]
    if isinstance(node.get("count"), int):
        out["count"] = node["count"]
    if ntype == "topology":
        out["ports"] = [vport_names.get(h, h) for h in node.get("ports") or []]
    if node.get("status"):
        out["status"] = node["status"]
    sc = rest.state_counts(node.get("stateCounts"))
    if sc and sc["total"]:
        out["sessions"] = sc
    kids = rest.child_nodes(node)
    if ntype == "ethernet":
        if node.get("useVlans"):
            out["vlans"] = node.get("vlanCount")
        else:  # vlan[1] always exists; only show it when VLANs are in use
            kids = [k for k in kids if rest.node_type(k["xpath"]) != "vlan"]
    if kids:
        if depth >= max_depth:
            out["children_not_shown"] = len(kids)
        else:
            out["children"] = [_tree(k, depth + 1, max_depth, vport_names) for k in kids]
    return out


def _count_status(tree: dict[str, Any], acc: dict[str, int]) -> None:
    if "sessions" in tree and tree.get("status"):
        acc[tree["status"]] = acc.get(tree["status"], 0) + 1
    for c in tree.get("children", []):
        _count_status(c, acc)


def overview(ixn: Any, sections: list[str], depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    vports = rest.get(ixn, "vport?includes=name,location,connectionState,actualSpeed,type")
    vport_names = {f"{rest.root(ixn)}/vport/{v['id']}": v["name"] for v in vports}
    if "ports" in sections:
        out["ports"] = [{"name": v["name"], "location": v.get("location") or None,
                         "state": v.get("connectionState"), "speed_mbps": v.get("actualSpeed"),
                         "type": v.get("type")} for v in vports]
    trees: list[dict[str, Any]] = []
    if "topologies" in sections or "status" in sections:
        for t in rest.get(ixn, "topology?includes=name"):
            href = f"{rest.root(ixn)}/topology/{t['id']}"
            r = rest.select(ixn, href, properties=_NODE_PROPS,
                            children=[rest.child(STACK_REGEX, _NODE_PROPS)])
            trees.append(_tree(r[0], 0, depth, vport_names))
        if "topologies" in sections:
            out["topologies"] = trees
    if "traffic" in sections or "status" in sections:
        items = rest.get(ixn, "traffic/trafficItem?includes=name,trafficType,trafficItemType,state,enabled,"
                              "biDirectional,errors,warnings")
        if "traffic" in sections:
            out["traffic_items"] = [{
                "name": i["name"], "type": i.get("trafficType"), "state": i.get("state"),
                "enabled": i.get("enabled"), "bidirectional": i.get("biDirectional"),
                "errors": i.get("errors") or [], "warnings": len(i.get("warnings") or []),
            } for i in items]
    if "status" in sections:
        protocol_status: dict[str, int] = {}
        for t in trees:
            _count_status(t, protocol_status)
        out["status"] = {
            "traffic_state": rest.get(ixn, "traffic?includes=state").get("state"),
            "apply_on_the_fly": rest.get(ixn, "globals/topology?includes=applyOnTheFlyState").get("applyOnTheFlyState"),
            "protocol_nodes_by_status": protocol_status,
            "ports_link_up": sum(1 for v in vports if v.get("connectionState") == "connectedLinkUp"),
            "ports_total": len(vports),
        }
    return out


def guide_topics() -> list[str]:
    return sorted(p.stem for p in GUIDE_DIR.glob("*.md"))


def read_guide(topic: str) -> str:
    path = GUIDE_DIR / f"{topic}.md"
    if not path.is_file():
        raise IxToolError(NOT_FOUND, f"no guide topic '{topic}'", candidates=guide_topics())
    return path.read_text()


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def list_targets() -> dict[str, Any]:
        """List the IxNetwork targets configured for this server (no secrets), with their guard
        flags and, once used, the attached IxNetwork build. Pass a target's `name` as `target`
        to other tools; it can be omitted when only one target is configured."""
        rows = []
        for ts in manager.sessions.values():
            row = ts.target.public()
            row.update({"connected": ts.connected, "build": ts.build, "platform": ts.platform})
            rows.append(row)
        return ok(None, rows)

    @app.tool(annotations=READ_ONLY)
    async def get_session_overview(
        target: str | None = None,
        sections: list[Section] | None = None,
        depth: int = 6,
    ) -> dict[str, Any]:
        """Summarize the live IxNetwork session: ports (vports with chassis location and link state),
        topologies as a tree (topology -> device groups -> protocol stack / network groups, with
        multipliers, device counts, status and session up/down counts), traffic items, and overall
        status. Start here before changing anything. `sections` defaults to all of
        ports, topologies, traffic, status; `depth` limits the topology tree depth."""
        wanted = list(sections or ["ports", "topologies", "traffic", "status"])
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: overview(ixn, wanted, max(1, depth)))
        result = {"session": {"build": ts.build, "platform": ts.platform, "host": ts.target.ip,
                              "session_id": ts.target.session_id}, **result}
        return ok(ts.target.name, result)

    @app.tool(annotations=READ_ONLY)
    async def get_guide(topic: str = "index") -> str:
        """Read the usage guide for this server (markdown). Topics: index (lists all topics),
        workflow, safety, troubleshooting, and more as features are added. Read `workflow` before
        building a configuration."""
        return read_guide(topic)

    for topic in guide_topics():
        def _make(t: str):
            def _resource() -> str:
                return read_guide(t)
            return _resource

        app.resource(f"guide://{topic}", name=f"guide-{topic}", mime_type="text/markdown",
                     description=f"IxNetwork MCP usage guide: {topic}")(_make(topic))
