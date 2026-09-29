"""apply_config: declarative, validated construction of topologies and protocol stacks."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import builder
from ..session import SessionManager
from .common import WRITE, ok
from .schema_tools import schema_for


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE)
    async def apply_config(spec: dict[str, Any], dry_run: bool = False, rollback_on_error: bool = True,
                           target: str | None = None) -> dict[str, Any]:
        """Build configuration declaratively. `spec` = {"topologies": [...], "extend": [...]}:
        topologies (name, ports, device_groups -> ethernet/vlans/ipv4/ipv6/bgp_peers/ospf/isis/
        igmp/mld/dhcp, loopbacks, network_groups with ipv4/ipv6 pools and bgp/ospf/isis route
        properties, chained device groups) and/or `extend` = [{"parent": <existing object ref>,
        "add": {...}}] to add children to existing objects. Read get_guide('spec') for every field
        and an example. Everything is validated first (placement, names, free ports, formats,
        enums, prerequisites) — nothing is written if anything is wrong; then each new subtree is
        created with one import, verified, and removed again if IxNetwork does not build it
        exactly (rollback_on_error). Use dry_run=true to preview the exact import entries."""
        schema, warnings = await schema_for(manager, target)
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: builder.apply(
            ixn, schema, spec, dry_run=dry_run, rollback_on_error=rollback_on_error), write=not dry_run)
        next_steps = None
        if not dry_run:
            next_steps = ["get_session_overview to review", "list_ports to check that the topology ports are up",
                          "save_config if the user wants a backup"]
        return ok(ts.target.name, result, warnings=warnings + result.pop("warnings", []), next_steps=next_steps)
