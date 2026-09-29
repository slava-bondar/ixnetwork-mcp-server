"""list_ports: session vports and (optionally) the chassis port inventory."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..session import SessionManager
from .common import READ_ONLY, ok

VPORT_FIELDS = "name,location,connectionState,connectionStatus,type,actualSpeed,assignedToDisplayName"
CHASSIS_PORT_FIELDS = ("location,description,owner,isAvailable,isBusy,isLinkUp,"
                       "peerSystemName,peerPortId,peerSystemIp,peerPortDescription")


def ports(ixn: Any, include_chassis: bool) -> dict[str, Any]:
    root = rest.root(ixn)
    vports = rest.get(ixn, f"vport?includes={VPORT_FIELDS}")
    used: dict[str, list[str]] = {}
    for t in rest.get(ixn, "topology?includes=name,ports"):
        for h in t.get("ports") or []:
            used.setdefault(h.rstrip("/"), []).append(t["name"])
    rows = []
    for v in vports:
        row: dict[str, Any] = {
            "name": v["name"],
            "location": v.get("location") or None,
            "state": v.get("connectionState"),
            "speed_mbps": v.get("actualSpeed"),
            "type": v.get("type"),
            "used_by_topologies": used.get(f"{root}/vport/{v['id']}", []),
        }
        if v.get("connectionState") not in ("connectedLinkUp",) and v.get("connectionStatus"):
            row["status_detail"] = v["connectionStatus"]
        rows.append(row)
    out: dict[str, Any] = {"vports": rows}
    if include_chassis:
        mine = {v.get("location"): v["name"] for v in vports if v.get("location")}
        chassis = []
        for loc in rest.get(ixn, "locations?includes=hostname,stateV2,deviceType,ixnBuildNumber,errorDescription"):
            entry: dict[str, Any] = {"hostname": loc.get("hostname"), "state": loc.get("stateV2"),
                                     "type": loc.get("deviceType"), "build": loc.get("ixnBuildNumber")}
            if loc.get("errorDescription"):
                entry["error"] = loc["errorDescription"]
            port_rows = []
            for p in rest.get(ixn, f"locations/{loc['id']}/ports?includes={CHASSIS_PORT_FIELDS}"):
                pr: dict[str, Any] = {
                    "location": p.get("location"),
                    "optic": p.get("description"),
                    "link_up": p.get("isLinkUp"),
                    "available": p.get("isAvailable"),
                    "owner": p.get("owner") or None,
                }
                if p.get("isBusy"):
                    pr["busy"] = True
                if p.get("location") in mine:
                    pr["vport"] = mine[p["location"]]
                peer = {k: p.get(src) for k, src in (("system", "peerSystemName"), ("port", "peerPortId"),
                                                     ("ip", "peerSystemIp"), ("port_description", "peerPortDescription"))
                        if p.get(src)}
                if peer:
                    pr["lldp_peer"] = peer
                port_rows.append(pr)
            entry["ports"] = port_rows
            chassis.append(entry)
        out["chassis"] = chassis
    return out


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=READ_ONLY)
    async def list_ports(include_chassis: bool = False, target: str | None = None) -> dict[str, Any]:
        """List the session's ports (vports): name, chassis location, connection/link state, speed,
        and which topologies use each one (a port can belong to only one topology).
        `include_chassis` adds the chassis inventory: every chassis port with optic, link state,
        availability, current owner (another user's session if taken), which of our vports maps to
        it, and the LLDP neighbor (DUT system/port) when known."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(target, lambda ixn: ports(ixn, include_chassis)))
