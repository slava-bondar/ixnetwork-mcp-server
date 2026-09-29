"""Runtime tools: assign_ports, unassign_ports, control_protocols."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .. import runtime
from ..session import SessionManager
from .common import DESTRUCTIVE, WRITE, ok, require_confirm


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE)
    async def assign_ports(mappings: list[dict[str, str]], create_missing: bool = True,
                           force_ownership: bool = False, confirm: bool = False, wait_link_up_s: int = 60,
                           target: str | None = None) -> dict[str, Any]:
        """Connect vports to chassis ports: mappings=[{"vport": "Ethernet - 001", "location":
        "192.0.2.20/1.1"}] (location as shown by list_ports(include_chassis=true)). Missing vports
        are created (create_missing). Chassis are shared with other projects' IxNetwork VMs: a port
        owned by another session is refused; taking it over needs force_ownership=true,
        confirm=true AND the target's guards.allow_port_takeover — ask the user first. Waits up to
        wait_link_up_s for link-up and reports each port's state."""
        ts = manager.resolve(target)
        if force_ownership:
            require_confirm(confirm, "taking over ports owned by another session")
        result = await manager.call(target, lambda ixn: runtime.assign_ports(
            ixn, mappings, create_missing=create_missing, force_ownership=force_ownership,
            allow_takeover=ts.target.guards.allow_port_takeover, wait_link_up_s=max(0, wait_link_up_s)),
            write=True)
        return ok(ts.target.name, result, warnings=result.pop("warnings", None))

    @app.tool(annotations=DESTRUCTIVE)
    async def unassign_ports(vports: list[str], confirm: bool = False, target: str | None = None) -> dict[str, Any]:
        """Release chassis ports from vports (the vports and their topologies stay). Traffic and
        protocols on these ports stop working. Ask the user, then call with confirm=true."""
        require_confirm(confirm, f"releasing {len(vports)} port(s)")
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(target, lambda ixn: runtime.unassign_ports(ixn, vports),
                                                     write=True))

    @app.tool(annotations=DESTRUCTIVE)
    async def control_protocols(action: Literal["start", "stop", "restart_down", "apply_changes"],
                                scope: str | None = None, wait_s: int = 60, confirm: bool = False,
                                target: str | None = None) -> dict[str, Any]:
        """Start / stop protocols, restart sessions that are down, or apply configuration changes to
        running protocols on the fly. `scope` = ref of a topology, device group or protocol object
        (omit for all; restart_down needs a scope). Protocols exchange packets with the device
        under test, so ask the user and pass confirm=true. Waits up to wait_s for sessions to come
        up (start) or go down (stop) and returns session counts per protocol and any problems."""
        require_confirm(confirm, f"{action.replace('_', ' ')} of protocols")
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: runtime.control_protocols(ixn, action, scope, max(0, wait_s)), write=True))
