"""Packet capture on vports.

Only the per-vport ``capture`` operations are used (``vport/<n>/capture/operations/start|stop``):
the root ``startCapture`` / ``stopCapture`` act on every port with capture enabled, which on a
shared session would include other people's ports. Data (hardware) capture needs the vport's
rxMode to include capture; a traffic Apply stops a running data capture, so arm it after Apply.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import rest
from .errors import NOT_FOUND, VALIDATION, IxToolError

MODES = {"data": "dataTraffic", "control": "controlTraffic", "both": "allTraffic"}
ENABLE = {"data": {"hardwareEnabled": True}, "control": {"softwareEnabled": True},
          "both": {"hardwareEnabled": True, "softwareEnabled": True}}
STATE_PROPS = ("hardwareEnabled,softwareEnabled,isCaptureRunning,isDataCaptureRunning,isControlCaptureRunning,"
               "dataPacketCounter,controlPacketCounter,dataCaptureState,controlCaptureState,sliceSize")
FIRST_PACKET = 0  # index of the first packet for getPacketFrom*Capture (verified live)
POLL_S = 1.0


def _vports(ixn: Any, names: list[str]) -> list[dict[str, Any]]:
    if not names:
        raise IxToolError(VALIDATION, "name at least one vport")
    rows = rest.get(ixn, "vport?includes=name,rxMode,location")
    by_name = {r["name"]: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise IxToolError(NOT_FOUND, f"unknown vport(s) {missing}", candidates=sorted(by_name))
    return [by_name[n] for n in names]


def _cap(ixn: Any, vport: dict[str, Any]) -> str:
    return f"{rest.root(ixn)}/vport/{vport['id']}/capture"


def state(ixn: Any, vport: dict[str, Any]) -> dict[str, Any]:
    s = rest.get(ixn, f"vport/{vport['id']}/capture?includes={STATE_PROPS}")
    return {"port": vport["name"], **{k: s.get(k) for k in STATE_PROPS.split(",")}}


def control(ixn: Any, action: str, ports: list[str], mode: str = "both",
            slice_size: int | None = None, wait_s: float = 30) -> dict[str, Any]:
    """start: enable data/control capture on the ports and start it; stop: stop it (the packets
    stay readable; returns once IxNetwork has processed them, so the counters are final);
    release: stop and disable capture on the ports."""
    if mode not in MODES:
        raise IxToolError(VALIDATION, f"mode must be one of {sorted(MODES)}")
    vps = _vports(ixn, ports)
    if action == "start":
        bad = [v["name"] for v in vps if mode != "control" and v.get("rxMode") not in ("capture", "captureAndMeasure")]
        if bad:
            raise IxToolError(VALIDATION, f"data capture needs rxMode 'captureAndMeasure' (or 'capture') on {bad}",
                              hint="Change the vport's rxMode with set_attributes if the user agrees; it also "
                                   "affects measurements on that port.")
    for v in vps:
        cap = _cap(ixn, v)
        if action == "start":
            ixn._connection._update(cap, {**ENABLE[mode], **({"sliceSize": slice_size} if slice_size else {})})
            ixn._connection._execute(f"{cap}/operations/start", {"arg1": cap, "arg2": MODES[mode]})
        elif action in ("stop", "release"):
            if action == "stop" or rest.get(ixn, f"vport/{v['id']}/capture?includes=isCaptureRunning").get("isCaptureRunning"):
                ixn._connection._execute(f"{cap}/operations/stop", {"arg1": cap, "arg2": MODES[mode]})
            if action == "release":
                ixn._connection._update(cap, {"hardwareEnabled": False, "softwareEnabled": False})
        else:
            raise IxToolError(VALIDATION, f"unknown action '{action}'", candidates=["start", "stop", "release"])
    if action == "stop":
        return {"action": action, "mode": mode, "ports": [_wait_ready(ixn, v, mode, wait_s) for v in vps]}
    return {"action": action, "mode": mode, "ports": [state(ixn, v) for v in vps]}


def running_data_captures(ixn: Any) -> list[str]:
    """Vports with a data capture running (a traffic Apply would stop it)."""
    out = []
    for v in rest.get(ixn, "vport?includes=name"):
        if rest.get(ixn, f"vport/{v['id']}/capture?includes=isDataCaptureRunning").get("isDataCaptureRunning"):
            out.append(v["name"])
    return out


def _wait_ready(ixn: Any, vport: dict[str, Any], mode: str, wait_s: float) -> dict[str, Any]:
    """After a stop IxNetwork processes the captured packets; the packet counters read 0 until the
    capture state is 'ready' (seen live). Waits for the enabled captures that `mode` covers."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        st = state(ixn, vport)
        keys = [key for m, key, enabled in (("data", "dataCaptureState", "hardwareEnabled"),
                                            ("control", "controlCaptureState", "softwareEnabled"))
                if mode in (m, "both") and (mode == m or st.get(enabled))]
        if all(st.get(k) == "ready" for k in keys) or time.monotonic() >= deadline:
            return st
        time.sleep(POLL_S)


def _useful(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop Wireshark's derived fields (resolved names, OUIs) and repeats of a value already shown
    (eth.addr repeats the destination and source)."""
    out, seen = [], set()
    for name, value in fields:
        if "(resolved)" in (name or "") or (name or "").endswith(" OUI") or value in seen:
            continue
        seen.add(value)
        out.append((name, value))
    return out


def digest(ixn: Any, port: str, mode: str = "control", start: int = 1, max_packets: int = 10,
           fields_per_header: int = 8, wait_s: float = 30) -> dict[str, Any]:
    """Decoded summary of captured packets `start`..(1-based): headers in packet order with
    their first non-empty fields."""
    if mode not in ("data", "control"):
        raise IxToolError(VALIDATION, "mode must be 'data' or 'control'")
    v = _vports(ixn, [port])[0]
    st = _wait_ready(ixn, v, mode, wait_s)
    if st.get("isCaptureRunning"):
        raise IxToolError(VALIDATION, f"capture is still running on '{port}'", hint="control_capture('stop') first.")
    total = int(st.get("dataPacketCounter" if mode == "data" else "controlPacketCounter") or 0)
    cur = f"{_cap(ixn, v)}/currentPacket"
    op = "getPacketFromDataCapture" if mode == "data" else "getPacketFromControlCapture"
    packets = []
    for n in range(max(1, start), min(total, max(1, start) + max_packets - 1) + 1):
        ixn._connection._execute(f"{cur}/operations/{op}", {"arg1": cur, "arg2": n - 1 + FIRST_PACKET})
        node = rest.select(ixn, cur, properties=["packetHex"],
                           children=[rest.child("^(stack|field)$", ["displayName", "fieldValue"])])[0]
        headers, when = [], None
        for s in node.get("stack") or []:
            fields = [(f.get("displayName"), f.get("fieldValue")) for f in s.get("field") or []
                      if f.get("fieldValue") not in (None, "")]
            if s.get("displayName") == "Frame":  # Wireshark's frame info: keep the arrival time only
                when = dict(fields).get("Arrival Time")
                continue
            headers.append({"header": s.get("displayName"), "fields": dict(_useful(fields)[:fields_per_header])})
        hexstr = "".join((node.get("packetHex") or "").split())
        packets.append({"packet": n, "time": when, "bytes": len(hexstr) // 2, "headers": headers})
    last = packets[-1]["packet"] if packets else 0
    return {"port": port, "mode": mode, "captured": total, "packets": packets,
            "truncated": total > last or None}


def save_files(ixn: Any, directory: Path) -> list[str]:
    """Save the current capture files on the VM (every port with a capture), download them into
    `directory` and delete them from the VM. Returns the local paths. IxNetwork names them
    `<port>_SW<suffix>.cap` (control) / `_HW` (data); the content is pcapng."""
    root = rest.root(ixn)
    listing = rest.get(ixn, "files")
    remote_dir = listing.get("absolute")
    before = {f.get("name") for f in listing.get("files") or []}
    suffix = f"mcp-{time.strftime('%Y%m%d-%H%M%S')}"
    ixn._connection._execute(f"{root}/operations/savecapturefiles", {"arg1": remote_dir, "arg2": suffix})
    new = [f.get("name") for f in rest.get(ixn, "files").get("files") or []
           if f.get("name") not in before and f.get("name", "").lower().endswith((".cap", ".pcap", ".pcapng"))]
    if not new:
        raise IxToolError(NOT_FOUND, "IxNetwork saved no capture files", hint="Was anything captured? Check get_capture.")
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name in new:
        local = directory / name
        try:
            ixn._connection._get_file(root, name, local_filename=str(local))
            out.append(str(local))
        finally:
            ixn._connection._delete_file(root, name)
    return out
