"""Crawl the IxNetwork REST schema via OPTIONS on type paths.

The server answers OPTIONS on a *type path* (ids are ignored, e.g.
``topology/deviceGroup/ethernet``) with a version-exact description of the node:
multiplicity, add/remove, children, attributes (types, enums, readOnly, multivalue
hrefs) and operations. The crawl is breadth-first; each generated class is expanded
once and later occurrences are stored as ``{"same_as": <first type path>}``.
A full crawl of a 10.00 build takes ~17 s (1,773 paths). Read-only.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import requests

log = logging.getLogger("ixnetwork_mcp.schema.crawl")

# (root type path, max depth below it; None = unlimited)
DEFAULT_SCOPE: list[tuple[str, int | None]] = [
    ("", 0),  # root node itself: root operations (newConfig, loadConfig, ...)
    ("topology", None),
    ("vport", 1),
    ("traffic", 0),
    ("traffic/trafficItem", None),
    ("globals/topology", 1),
    ("resourceManager", 0),
    ("statistics", 1),
    ("statistics/view", 2),
    ("availableHardware", 2),
    ("locations", 2),
    ("lag", 0),
    ("quickTest", 0),  # QuickTest families the tools support (each family is a large subtree)
    ("quickTest/rfc2544throughput", None),
    ("quickTest/rfc2544frameLoss", None),
    ("quickTest/rfc2544back2back", None),
]

# Children that are pure plumbing and would multiply the crawl without adding value.
SKIP_CHILDREN = {"port", "genericProtocol"}
# Packet/encapsulation template catalogs (genericProtocol/encapsulations/*,
# openFlowSwitch/.../protocolEncapsulationList/*): hundreds of nodes at 5-15 s per OPTIONS
# and irrelevant to NGPF protocol configuration.
SKIP_CHILD_RE = re.compile(r"(?i)encapsulation")


class Crawler:
    def __init__(self, base: str, *, headers: dict[str, str] | None = None, verify: bool = False,
                 workers: int = 4, timeout: float = 15):
        self.base = base.rstrip("/")
        self.workers = workers
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        if headers:
            self.session.headers.update(headers)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self.slow: dict[str, float] = {}
        self.requests = 0
        self.signatures: dict[tuple, str] = {}
        self.duplicates = 0
        self.checkpoint: Callable[["Crawler"], None] | None = None

    def options(self, type_path: str) -> dict[str, Any] | None:
        url = f"{self.base}/{type_path}" if type_path else self.base
        for attempt in range(2):
            t0 = time.time()
            try:
                self.requests += 1
                resp = self.session.options(url, timeout=self.timeout)
                elapsed = time.time() - t0
                if elapsed > 5:
                    self.slow[type_path] = round(elapsed, 1)
                    log.warning("slow OPTIONS %.1fs %s", elapsed, type_path)
                if resp.status_code == 200:
                    self.errors.pop(type_path, None)
                    return resp.json()
                self.errors[type_path] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code != 400 or attempt:
                    return None
                time.sleep(1)  # parallel OPTIONS on quickTest families sometimes fail with a transient 400
            except (requests.RequestException, ValueError) as exc:
                self.errors[type_path] = f"{type(exc).__name__}: {exc}"
                log.warning("OPTIONS error (%d/2) %s: %s", attempt + 1, type_path, type(exc).__name__)
        return None

    def crawl(self, scope: list[tuple[str, int | None]] = DEFAULT_SCOPE) -> None:
        frontier: list[tuple[str, int | None, tuple[str, ...]]] = []
        for root, depth in scope:
            ancestors = tuple(p for p in root.split("/") if p)
            frontier.append((root, depth, ancestors[:-1]))
        level = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while frontier:
                seen: set[str] = set()
                todo = [f for f in frontier if f[0] not in self.nodes and not (f[0] in seen or seen.add(f[0]))]
                t0 = time.time()
                results = list(pool.map(lambda f: (f, self.options(f[0])), todo))
                log.info("level %d: %d nodes in %.1fs (total %d, dup %d, errors %d)", level, len(todo),
                         time.time() - t0, len(self.nodes) + len(todo), self.duplicates, len(self.errors))
                frontier = []
                for (path, depth_left, ancestors), doc in results:
                    if doc is not None:
                        frontier.extend(self._add(path, depth_left, ancestors, doc.get("custom") or {}))
                level += 1
                if self.checkpoint:
                    self.checkpoint(self)

    def _add(self, path: str, depth_left: int | None, ancestors: tuple[str, ...],
             custom: dict[str, Any]) -> list[tuple[str, int | None, tuple[str, ...]]]:
        sig = (
            custom.get("name"),
            tuple(sorted(c["name"] for c in custom.get("children") or [])),
            tuple(sorted(a["name"] for a in custom.get("attributes") or [])),
        )
        first = self.signatures.get(sig)
        if first is not None and first != path:
            self.nodes[path] = {"name": custom.get("name"), "same_as": first}
            self.duplicates += 1
            return []
        self.signatures[sig] = path
        node = {k: custom.get(k) for k in (
            "name", "path", "multiplicity", "add", "remove", "required", "deprecated", "internal", "description")}
        node["children"] = []
        node["attributes"] = custom.get("attributes") or []
        node["operations"] = custom.get("operations") or []
        self.nodes[path] = node
        my_name = custom.get("name")
        nxt = []
        for child in custom.get("children") or []:
            cname = child["name"]
            cpath = f"{path}/{cname}" if path else cname
            edge: dict[str, Any] = {"name": cname, "type_path": cpath}
            if cname in SKIP_CHILDREN or SKIP_CHILD_RE.search(cname):
                edge["skipped"] = True
            elif cname in ancestors or cname == my_name:
                # recursion (deviceGroup/deviceGroup, networkGroup/networkGroup ...):
                # point at the nearest ancestor of the same name instead of expanding.
                parts = path.split("/")
                idx = max(i for i, p in enumerate(parts) if p == cname)
                edge["same_as"] = "/".join(parts[: idx + 1])
            elif depth_left is not None and depth_left <= 0:
                edge["not_crawled"] = True
            else:
                nd = None if depth_left is None else depth_left - 1
                nxt.append((cpath, nd, ancestors + ((my_name,) if my_name else ())))
            node["children"].append(edge)
        return nxt

    def document(self, build: str, source: str, started: float, complete: bool) -> dict[str, Any]:
        return {
            "meta": {
                "build": build,
                "source": source,
                "crawled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "complete": complete,
                "scope": DEFAULT_SCOPE,
                "node_count": len(self.nodes),
                "unique_classes": len(self.signatures),
                "requests": self.requests,
                "seconds": round(time.time() - started, 1),
                "errors": self.errors,
                "slow": self.slow,
            },
            "nodes": self.nodes,
        }


def write_doc(doc: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
    tmp.replace(path)


def crawl_to_file(base: str, build: str, out: Path, *, headers: dict[str, str] | None = None,
                  verify: bool = False, workers: int = 4, timeout: float = 15,
                  checkpoint_path: Path | None = None) -> dict[str, Any]:
    started = time.time()
    c = Crawler(base, headers=headers, verify=verify, workers=workers, timeout=timeout)
    if checkpoint_path is not None:
        c.checkpoint = lambda cr: write_doc(cr.document(build, base, started, False), checkpoint_path)
    c.crawl()
    doc = c.document(build, base, started, True)
    write_doc(doc, out)
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint_path.unlink()
    log.info("schema crawl done: %d nodes, %d classes, %d errors, %.0fs -> %s",
             len(c.nodes), len(c.signatures), len(c.errors), time.time() - started, out)
    return doc["meta"]
