"""Packet capture tools: control_capture, get_capture."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .. import capture
from ..session import SessionManager
from .common import READ_ONLY, WRITE, ok


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE)
    async def control_capture(action: Literal["start", "stop", "release", "save"], ports: list[str] | None = None,
                              mode: Literal["data", "control", "both"] = "both", slice_size: int | None = None,
                              target: str | None = None) -> dict[str, Any]:
        """Packet capture on the named vports: 'start' enables and starts data (hardware) and/or
        control (protocol) capture, 'stop' stops it (packets stay readable with get_capture),
        'release' stops and disables it, 'save' downloads the capture files of all captured ports
        to ~/.ixnetwork-mcp/exports/<target>/ (Wireshark). A traffic Apply stops a running data
        capture: apply traffic first, then start the capture, then start traffic."""
        ts = manager.resolve(target)
        if action == "save":
            paths = await manager.call(target, lambda ixn: capture.save_files(ixn, ts.target.exports_dir), write=True)
            return ok(ts.target.name, {"files": paths})
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: capture.control(ixn, action, ports or [], mode, slice_size), write=True))

    @app.tool(annotations=READ_ONLY)
    async def get_capture(port: str, mode: Literal["data", "control"] = "control", start: int = 1,
                          max_packets: int = 10, target: str | None = None) -> dict[str, Any]:
        """Decoded digest of captured packets on one vport after control_capture('stop'): packet
        count and, for packets start..start+max_packets-1, the headers in order with their first
        fields (e.g. Ethernet, VLAN, IPv4, BGP). Use control_capture('save') for the full files."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: capture.digest(ixn, port, mode, start, max(1, min(max_packets, 50)))))
