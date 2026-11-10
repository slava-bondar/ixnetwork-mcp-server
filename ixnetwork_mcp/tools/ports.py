"""list_ports: session vports and (optionally) the chassis port inventory."""

from __future__ import annotations

import logging
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import rest
from ..session import SessionManager
from .common import READ_ONLY, ok

log = logging.getLogger("ixnetwork_mcp.ports")

VPORT_FIELDS = "name,location,connectionState,connectionStatus,type,actualSpeed,assignedToDisplayName"
CHASSIS_PORT_FIELDS = ("location,description,owner,isAvailable,isBusy,isLinkUp,"
                       "peerSystemName,peerPortId,peerSystemIp,peerPortDescription")


# L1 settings worth showing; which of them exist depends on the card type
L1_PROPS = ["speed", "selectedSpeeds", "media", "autoNegotiate", "enableAutoNegotiation", "ieeeL1Defaults",
            "enableRsFec", "rsFecForceOn", "firecodeForceOn", "forceDisableFEC", "linkTraining", "loopback",
            "loopbackMode", "enabledFlowControl"]


def _one(value: Any) -> dict[str, Any]:
    """A singleton child of a select result (returned as an object, or as a one-item list)."""
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def l1_settings(ixn: Any, vports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """vport href -> {type, xpath, <settings>} of each port's active L1 node (the card-type child of
    its l1Config that currentType names); the xpath is what get_node / set_attributes take."""
    kinds = sorted({(v.get("type") or "").removesuffix("Fcoe") for v in vports} - {""})
    if not kinds or not all(re.fullmatch(r"[A-Za-z0-9]+", k) for k in kinds):
        return {}
    try:
        sel = rest.select(ixn, rest.root(ixn), properties=[], children=[
            rest.child(f"^(vport|l1Config|{'|'.join(kinds)})$", ["currentType", *L1_PROPS])])
    except Exception as exc:  # noqa: BLE001 - the port list must not fail over its L1 summary
        log.debug("L1 summary not available: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for vp in sel[0].get("vport") or []:
        l1 = _one(vp.get("l1Config"))
        current = l1.get("currentType") or ""
        node = _one(l1.get(current.removesuffix("Fcoe")))
        if node.get("xpath"):
            out[str(vp.get("href", "")).rstrip("/")] = {
                "type": current, "xpath": node["xpath"],
                **{k: node[k] for k in L1_PROPS if node.get(k) not in (None, "", [])}}
    return out


def ports(ixn: Any, include_chassis: bool) -> dict[str, Any]:
    root = rest.root(ixn)
    vports = rest.get(ixn, f"vport?includes={VPORT_FIELDS}")
    l1 = l1_settings(ixn, vports)
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
        if f"{root}/vport/{v['id']}" in l1:
            row["l1"] = l1[f"{root}/vport/{v['id']}"]
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
        L1 settings (`l1`: speed, FEC, auto-negotiation ... with the xpath to change them through
        set_attributes), and which topologies use each one (a port can belong to only one topology).
        `include_chassis` adds the chassis inventory: every chassis port with optic, link state,
        availability, current owner (another user's session if taken), which of our vports maps to
        it, and the LLDP neighbor (DUT system/port) when known."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(target, lambda ixn: ports(ixn, include_chassis)))
