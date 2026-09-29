"""Traffic tools: get_traffic_items, configure_traffic_items, describe_packet_headers,
control_traffic, get_stats."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from .. import runtime
from ..errors import GUARD_BLOCKED, IXN_ERROR, NOT_FOUND, VALIDATION, IxToolError
from ..refs import ConfigIndex
from ..session import SessionManager
from ..traffic import engine, stats
from ..traffic.models import TrafficItemConfig
from .common import DESTRUCTIVE, READ_ONLY, WRITE, ok, require_confirm


def _parse(items: list[dict[str, Any]]) -> list[TrafficItemConfig]:
    parsed, errors = [], []
    for i, raw in enumerate(items):
        try:
            parsed.append(TrafficItemConfig.model_validate(raw))
        except ValidationError as exc:
            errors += [f"items[{i}].{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
    names = [p.name for p in parsed]
    errors += [f"traffic item name '{n}' appears more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    if errors:
        raise IxToolError(VALIDATION, f"{len(errors)} problem(s) in the traffic items; nothing was changed",
                          errors=errors, hint="get_guide('traffic') describes the fields.")
    return parsed


def configure(ixn: Any, cfgs: list[TrafficItemConfig], mode: str, dry_run: bool) -> dict[str, Any]:
    index = ConfigIndex.build(ixn, props=["name"])
    plans, problems = [], []
    for cfg in cfgs:
        exists = engine.find_item(ixn, cfg.name) is not None
        action = "update" if exists else "create"
        if mode == "create" and exists:
            problems.append(f"{cfg.name}: a traffic item with this name exists (use mode='update' or 'upsert')")
        elif mode == "update" and not exists:
            problems.append(f"{cfg.name}: no traffic item with this name (use mode='create' or 'upsert')")
        try:
            engine.resolve_endpoint_sets(index, cfg)
        except engine.TrafficProblems as exc:
            problems += exc.problems
        plans.append((cfg, action))
    templates = engine.Templates(ixn)
    for cfg, _ in plans:
        for i, ce in enumerate(cfg.config_elements):
            for j, h in enumerate(ce.stack):
                if templates.find(h.protocol)[0] is None:
                    problems.append(f"{cfg.name}.config_elements[{i}].stack[{j}]: unknown protocol '{h.protocol}' "
                                    f"(e.g. {templates.find(h.protocol)[1]})")
    if problems:
        raise IxToolError(VALIDATION, f"{len(problems)} problem(s); nothing was changed", errors=problems[:40],
                          hint="Endpoints are object names (find_nodes); packet headers from describe_packet_headers.")
    if dry_run:
        return {"dry_run": True, "valid": True, "plan": [{"name": c.name, "action": a} for c, a in plans]}
    results, failed = [], []
    for cfg, action in plans:
        try:
            if action == "create":
                results.append(engine.create(ixn, cfg, index, templates))
            else:
                results.append(engine.update(ixn, cfg, engine.find_item(ixn, cfg.name), index, templates))
        except engine.TrafficProblems as exc:
            failed.append({"name": cfg.name, "action": action, "problems": exc.problems[:20],
                           "note": "removed again" if action == "create" else "may be partially updated"})
    if failed:
        raise IxToolError(IXN_ERROR, f"{len(failed)} of {len(plans)} traffic item(s) failed", failed=failed,
                          succeeded=results or None)
    return {"results": results}


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def get_traffic_items(names: list[str] | None = None, detail: Literal["summary", "full"] = "summary",
                                target: str | None = None) -> dict[str, Any]:
        """List traffic items: state, endpoints (as object names), rate, frame size, transmission
        mode, tracking and errors. detail='full' returns each item in the configure_traffic_items
        format (endpoints, config elements with packet header overrides, tracking) so it can be
        edited and sent back with mode='update'."""
        ts = manager.resolve(target)

        def run(ixn):
            index = ConfigIndex.build(ixn, props=["name"])
            if detail == "summary":
                return {"traffic_items": engine.summarize(ixn, index, names)}
            out = []
            for n in names or [x.name for x in index.nodes if x.type == "trafficItem"]:
                ti = engine.find_item(ixn, n)
                if ti is None:
                    raise IxToolError(NOT_FOUND, f"no traffic item '{n}'")
                out.append(engine.read_full(ixn, index, ti))
            return {"traffic_items": out}

        return ok(ts.target.name, await manager.call(target, run))

    @app.tool(annotations=WRITE)
    async def configure_traffic_items(items: list[dict[str, Any]], mode: Literal["create", "update", "upsert"] = "upsert",
                                      dry_run: bool = False, target: str | None = None) -> dict[str, Any]:
        """Create or update traffic items. Each item: {"name", "traffic_type": ipv4|ipv6|ethernetVlan|raw,
        "bi_directional", "src_dest_mesh", "endpoint_sets": [{"sources": [{"ref": "<object name>"}],
        "destinations": [...]} or "multicast_destinations": ["225.0.0.1/0.0.0.1/4"]],
        "config_elements": [{"frame_rate": {"type": "percentLineRate", "rate": 10}, "frame_size":
        {"type": "fixed", "fixed_size": 512}, "transmission": {"type": "continuous"}, "stack":
        [{"protocol": "udp", "fields": [{"field_name": "UDP-Source-Port", "single_value": "1234"}]}]}],
        "flow_tracking": {"track_by": ["trackingenabled0", "sourceDestEndpointPair0"]}}.
        Endpoints are objects by name: ipv4/ipv6 stacks, network groups (route pools), dhcp clients,
        device groups or topologies (find_nodes). Everything is resolved first; nothing changes if
        anything is wrong. Items are generated; a warning about unresolved MACs is normal until
        protocols are started. Read get_guide('traffic') for all fields."""
        cfgs = _parse(items)
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: configure(ixn, cfgs, mode, dry_run), write=not dry_run)
        return ok(ts.target.name, result)

    @app.tool(annotations=READ_ONLY)
    async def describe_packet_headers(template: str | None = None, query: str | None = None,
                                      target: str | None = None) -> dict[str, Any]:
        """Packet header templates for traffic item stacks. Without `template`: list templates
        (filter with `query`, e.g. 'vxlan', 'tcp'). With `template` (e.g. 'udp'): its fields with
        display names (use as field_name), formats, defaults, enum values and allowed value types."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(target, lambda ixn: engine.packet_headers(ixn, template, query)))

    @app.tool(annotations=DESTRUCTIVE)
    async def control_traffic(action: Literal["generate", "apply", "start", "stop", "clear_stats"],
                              items: list[str] | None = None, wait_s: int = 30, confirm: bool = False,
                              target: str | None = None) -> dict[str, Any]:
        """Traffic runtime: 'generate' rebuilds flows (after protocols are up so MACs resolve),
        'apply' pushes them to the ports, 'start' / 'stop' (all, or the named `items`),
        'clear_stats'. start sends packets to the device under test: it needs the target's
        guards.allow_traffic_start and confirm=true after asking the user (it generates and applies
        first when needed). Returns traffic state and per-item states."""
        ts = manager.resolve(target)
        if action == "start":
            if not ts.target.guards.allow_traffic_start:
                raise IxToolError(GUARD_BLOCKED, f"starting traffic is disabled for target '{ts.target.name}'")
            require_confirm(confirm, "starting traffic")
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: runtime.control_traffic(ixn, action, items, max(0, wait_s)), write=True))

    @app.tool(annotations=READ_ONLY)
    async def get_stats(view: str | None = None, filters: dict[str, str] | None = None,
                        columns: list[str] | None = None, limit: int = 100,
                        target: str | None = None) -> dict[str, Any]:
        """Read a statistics view, e.g. 'Port Statistics', 'Traffic Item Statistics',
        'Flow Statistics', 'Protocols Summary' (without `view`: list the available views).
        `filters` = {column: regex} keeps matching rows (e.g. {"Traffic Item": "^v4"}); `columns`
        selects columns; numbers are returned as numbers. Pages through the whole view and puts the
        GUI's view back on the page it was on."""
        ts = manager.resolve(target)
        if view is None:
            return ok(ts.target.name, {"views": await manager.call(target, stats.list_views)})
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: stats.read_view(ixn, view, filters, columns, max(1, min(limit, 5000)))))
