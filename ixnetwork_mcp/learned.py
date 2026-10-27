"""Learned information of running protocols (routes, LSAs, adjacencies): BGP, OSPFv2/v3, IS-IS.

A ``get*LearnedInfo`` operation makes the protocol (on its port) refresh the object's
``learnedInfo`` children. Each child is ``{type, state, columns, values}`` and may hold ``table``
children of the same shape. The operations change no configuration, so they are reads
(``compiler.is_read_operation``).
"""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from typing import Any

from . import rest
from .errors import VALIDATION, IxToolError
from .refs import ConfigIndex, Node, parse_device_ranges
from .traffic.stats import _coerce

_BGP = {"all": "getAllLearnedInfo", "ipv4": "getIPv4LearnedInfo", "ipv6": "getIPv6LearnedInfo",
        "evpn": "getEVPNLearnedInfo", "linkstate": "getLinkStateLearnedInfo"}
_OSPF = {"all": "getBasicLearnedInfo", "basic": "getBasicLearnedInfo", "detailed": "getDetailedLearnedInfo"}
OPERATIONS: dict[str, dict[str, str]] = {  # node type -> kind -> operation (all verified in the 10.00 schema)
    "bgpIpv4Peer": {**_BGP, "vpn": "getIPv4VpnLearnedInfo"},
    "bgpIpv6Peer": {**_BGP, "vpn": "getIPv6VpnLearnedInfo"},
    "ospfv2": _OSPF,
    "ospfv3": _OSPF,
    "isisL3": {"all": "getLearnedInfo"},
}
KINDS = sorted({k for ops in OPERATIONS.values() for k in ops})
POLL_S = 1.0


def _targets(index: ConfigIndex, ref: str) -> list[Node]:
    node = index.resolve(ref)
    if node.type in OPERATIONS:
        return [node]
    found = [n for n in index.subtree(node) if n.type in OPERATIONS]
    if not found:
        raise IxToolError(VALIDATION, f"no BGP, OSPF or IS-IS object under '{ref}'",
                          hint="Pass a BGP peer, OSPF or IS-IS object, or a device group/topology that has one.")
    return found


def _tables(ixn: Any, href: str, wait_s: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        node = rest.select(ixn, href, properties=[], children=[rest.child("^(learnedInfo|table)$")])[0]
        infos = node.get("learnedInfo") or []
        if infos or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)
    out: list[dict[str, Any]] = []
    for li in infos:
        for t in [li, *(li.get("table") or [])]:
            cols = [str(c) for c in t.get("columns") or []]
            if not cols:
                continue
            rows = [dict(zip(cols, (_coerce(v) for v in row))) for row in t.get("values") or []]
            out.append({"table": t.get("type") or li.get("type"), "state": li.get("state"), "columns": cols,
                        "rows": rows})
    return out


def collect(ixn: Any, ref: str, kind: str = "all", devices: str | None = None,
            wait_s: float = 10.0) -> list[dict[str, Any]]:
    """Refresh and read learned info of every BGP/OSPF/IS-IS object at or under `ref`. Every row
    is returned; `shape` trims for the agent."""
    if kind not in KINDS:
        raise IxToolError(VALIDATION, f"unknown kind '{kind}'", candidates=KINDS)
    nodes = _targets(ConfigIndex.build(ixn), ref)
    out: list[dict[str, Any]] = []
    for node in nodes:
        ops = OPERATIONS[node.type]
        if kind not in ops:
            if len(nodes) == 1:
                raise IxToolError(VALIDATION, f"kind '{kind}' does not apply to {node.type}", candidates=sorted(ops))
            continue
        payload: dict[str, Any] = {"arg1": [node.href]}
        if devices:
            payload["sessionIndices"] = parse_device_ranges(devices, node.props.get("count"))
        ixn._connection._execute(f"{node.href}/operations/{ops[kind]}", payload)
        out.append({"protocol": node.name, "type": node.type, "xpath": node.xpath, "operation": ops[kind],
                    "tables": _tables(ixn, node.href, wait_s)})
    if not out:
        raise IxToolError(VALIDATION, f"kind '{kind}' applies to none of the objects under '{ref}'",
                          candidates=sorted({k for n in nodes for k in OPERATIONS[n.type]}))
    return out


def shape(results: list[dict[str, Any]], columns: list[str] | None, filters: dict[str, str] | None,
          max_rows: int) -> list[dict[str, Any]]:
    """Filter ({column: regex}, case-insensitive), project columns and cap rows per table."""
    all_cols = sorted({c for r in results for t in r["tables"] for c in t["columns"]})
    for col in [*(filters or {}), *(columns or [])]:
        if all_cols and col not in all_cols:
            raise IxToolError(VALIDATION, f"no learned-info table has a column '{col}'", candidates=all_cols)
    try:
        compiled = {c: re.compile(rx, re.IGNORECASE) for c, rx in (filters or {}).items()}
    except re.error as exc:
        raise IxToolError(VALIDATION, f"bad filter regex: {exc}") from exc
    shaped = []
    for r in results:
        tables = []
        for t in r["tables"]:
            rows = [row for row in t["rows"]
                    if all(c in row and rx.search(str(row[c])) for c, rx in compiled.items())]
            keep = [c for c in (columns or t["columns"]) if c in t["columns"]]
            tables.append({"table": t["table"], "state": t["state"], "columns": keep, "total_rows": len(t["rows"]),
                           "matched": len(rows), "rows": [{c: row.get(c) for c in keep} for row in rows[:max_rows]],
                           "truncated": len(rows) > max_rows or None})
        shaped.append({**{k: v for k, v in r.items() if k != "tables"}, "tables": tables})
    return shaped


def write_csv(results: list[dict[str, Any]], directory: Path) -> list[str]:
    """One CSV per non-empty table (every row); returns the paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for r in results:
        for t in r["tables"]:
            if not t["rows"]:
                continue
            slug = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{r['protocol']}-{t['table']}")[:120]
            path = directory / f"learned-{stamp}-{slug}.csv"
            with path.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=t["columns"], extrasaction="ignore")
                w.writeheader()
                w.writerows(t["rows"])
            paths.append(str(path))
    return paths
