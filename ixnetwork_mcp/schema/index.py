"""Schema index built from ``cache/<build>.json.gz`` (the REST schema, crawled with OPTIONS by
``crawl.py``) and ``data/mv_meta_<build>.json`` (multivalue formats/enums/defaults read from live
objects).

The crawl stores each generated class once; other occurrences are ``{"same_as": path}``.
A *canonical path* is the type path where a class was expanded. Lookups follow
``same_as`` segment by segment, so ``topology/deviceGroup/ethernet/ipv4/igmpHost/…`` resolves
even though ``igmpHost`` was first expanded under another parent.
"""

from __future__ import annotations

import functools
import gzip
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("ixnetwork_mcp.schema")
_learn_lock = threading.Lock()

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"  # shipped with the package
DATA_DIR = HERE / "data"
USER_CACHE_DIR = Path("~/.ixnetwork-mcp/schema-cache").expanduser()  # crawled at runtime for other builds


@dataclass(frozen=True)
class NodeInfo:
    canonical: str
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return self.raw["name"]


class SchemaIndex:
    def __init__(self, nodes: dict[str, dict[str, Any]], mv_meta: dict[str, dict[str, Any]], build: str,
                 mv_build: str | None = None, *, learned: dict[str, dict[str, Any]] | None = None,
                 learn_key: str | None = None):
        self.nodes = nodes
        self.build = build
        self.mv_build = mv_build
        self.learn_key = learn_key or build       # the build whose live objects teach this index
        self.mv_exact = mv_build == self.learn_key  # the shipped multivalue data was captured on that very build
        self.by_name: dict[str, list[str]] = {}
        for path, node in nodes.items():
            name = node.get("name") or path.rsplit("/", 1)[-1]
            self.by_name.setdefault(name, []).append(path)
        # multivalue metadata is keyed by concrete type paths; fold onto canonical classes. Data
        # captured on this build wins over what was learned; data from another build does not.
        self.mv: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, set[str]] = {}
        # classes whose live objects were read in full: captured on this very build, or learned before
        self._complete: set[str] = set()
        for path in [*(mv_meta if self.mv_exact else {}), *(learned or {})]:
            info = self.resolve(path)
            if info is not None:
                self._complete.add(info.canonical)
        for source in ([mv_meta, learned or {}] if self.mv_exact else [learned or {}, mv_meta]):
            for path, attrs in source.items():
                info = self.resolve(path)
                if info is None:
                    continue
                folded = self.mv.setdefault(info.canonical, {})
                for attr, meta in attrs.items():  # merge per key: a capture without a default keeps another's
                    cur = folded.setdefault(attr, {})
                    for k, v in meta.items():
                        cur.setdefault(k, v)

    # ------------------------------------------------------------------ learning from live objects

    def learn_multivalues(self, type_path: str, node: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Take format, enums and allowed patterns of the per-device attributes of a live object
        (a select answer with rest.MV_INLINES) into the index: the REST schema does not publish
        them. -> the rows that were new, keyed by attribute. No names or values are kept."""
        info = self.resolve(type_path)
        if info is None:
            return {}
        known = self.mv.setdefault(info.canonical, {})
        seen = self._seen.setdefault(info.canonical, set())
        new: dict[str, dict[str, Any]] = {}
        for attr, mv in node.items():
            if attr in seen or not (isinstance(mv, dict) and "/multivalue[" in str(mv.get("xpath", ""))):
                continue
            fmt = mv.get("format")
            if not fmt or fmt == "kVoid":
                continue
            seen.add(attr)
            have = known.get(attr, {})
            if self.mv_exact and have.get("format"):
                continue  # captured for this very build already
            row: dict[str, Any] = {"format": fmt, "availablePatterns": list(mv.get("availablePatterns") or [])}
            if mv.get("enums"):
                row["enums"] = list(mv["enums"])
            if have.get("format") == fmt and (have.get("enums") or None) == row.get("enums"):
                continue  # nothing the index did not have
            known[attr] = {**{k: v for k, v in have.items() if k != "enums"}, **row}
            new[attr] = row
        return new

    def needs_full_read(self, type_path: str) -> bool:
        """Has no live object of this type been read in full yet (on the target's own build)?"""
        info = self.resolve(type_path)
        return info is not None and info.canonical not in self._complete

    def remember(self, type_path: str, node: dict[str, Any]) -> None:
        """learn_multivalues from a full read of a live object, and keep what was new in the user's
        cache for this build. Never raises: a read must not fail because the cache cannot be written."""
        try:
            info = self.resolve(type_path)
            if info is None:
                return
            first = info.canonical not in self._complete
            new = self.learn_multivalues(type_path, node)
            self._complete.add(info.canonical)
            if new or first:  # also note a type that taught nothing, so it is not read again next time
                _store_learned(self.learn_key, info.canonical, new)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not keep learned multivalue formats for %s: %s", type_path, exc)

    # ------------------------------------------------------------------ lookup

    def resolve(self, type_path: str) -> NodeInfo | None:
        cur = ""
        node: dict[str, Any] | None = None
        for seg in type_path.strip("/").split("/"):
            nxt = f"{cur}/{seg}" if cur else seg
            if nxt in self.nodes:
                cur, node = nxt, self.nodes[nxt]
            elif node is not None:
                # recursion edges (networkGroup/deviceGroup, deviceGroup/deviceGroup ...) are stored
                # on the parent's child list as {"same_as": <ancestor path>}
                edge = next((c for c in node.get("children") or [] if c["name"] == seg), None)
                if edge is not None and edge.get("same_as"):
                    cur, node = edge["same_as"], self.nodes.get(edge["same_as"])
                else:
                    return None
            else:
                cur = nxt  # prefix outside the crawl scope (e.g. 'globals' above 'globals/topology')
                continue
            hops = 0
            while node is not None and "same_as" in node and hops < 20:
                cur = node["same_as"]
                node = self.nodes.get(cur)
                hops += 1
        return NodeInfo(cur, node) if node is not None and "same_as" not in node else None

    def lookup(self, node_type: str) -> tuple[NodeInfo | None, list[str]]:
        """Accept a type path ('topology/deviceGroup/ethernet') or a bare type name
        ('bgpIpv4Peer'). Returns (info, all type paths where that type occurs)."""
        node_type = node_type.strip().strip("/")
        if "/" in node_type:
            info = self.resolve(node_type)
            return info, [node_type] if info else []
        paths = self.by_name.get(node_type)
        if not paths:
            ci = {k.lower(): k for k in self.by_name}
            key = ci.get(node_type.lower())
            paths = self.by_name.get(key, []) if key else []
        if not paths:
            return None, []
        resolved = [(p, self.resolve(p)) for p in paths]
        infos = {i.canonical: i for _, i in resolved if i is not None}
        # prefer the NGPF topology class when the same name exists in several subtrees
        best = sorted(infos.values(), key=lambda i: (not i.canonical.startswith("topology"), len(i.canonical)))
        if not best:
            return None, []
        same_class = [p for p, i in resolved if i is not None and i.canonical == best[0].canonical]
        return best[0], sorted(same_class, key=_path_preference)

    def attribute_meta(self, type_path: str) -> dict[str, dict[str, Any]]:
        """Raw schema attributes of a type keyed by name, each with an added 'kind'
        (multivalue | settable | readonly), plus multivalue metadata under 'mv' when captured."""
        info = self.resolve(type_path)
        if info is None:
            return {}
        mv = self.mv.get(info.canonical, {})
        out: dict[str, dict[str, Any]] = {}
        for a in info.raw.get("attributes") or []:
            t = a.get("type") or {}
            is_mv = t.get("name") == "href" and any(h.endswith("/multivalue") for h in t.get("hrefs") or [])
            row = dict(a)
            row["kind"] = "multivalue" if is_mv else ("readonly" if a.get("readOnly") else "settable")
            if is_mv and a["name"] in mv:
                row["mv"] = mv[a["name"]]
            out[a["name"]] = row
        return out

    def child_type_path(self, parent_type_path: str, child: str) -> str | None:
        """Type path of ``child`` under ``parent_type_path`` if the schema allows it there."""
        if not parent_type_path:
            root = self.nodes.get("")
            names = {c["name"] for c in (root or {}).get("children") or []}
            return child if child in names else None
        info = self.resolve(parent_type_path)
        if info is None:
            return None
        for c in info.raw.get("children") or []:
            if c["name"] == child:
                return f"{parent_type_path}/{child}"
        return None

    # ------------------------------------------------------------------ describe

    def attribute_rows(self, info: NodeInfo, *, include_readonly: bool = False,
                       names: list[str] | None = None) -> list[dict[str, Any]]:
        mv = self.mv.get(info.canonical, {})
        wanted = [n.lower() for n in names] if names else None
        rows = []
        for a in info.raw.get("attributes") or []:
            if a.get("deprecated") or a.get("internal"):
                continue
            t = a.get("type") or {}
            is_mv = t.get("name") == "href" and any(h.endswith("/multivalue") for h in t.get("hrefs") or [])
            kind = "multivalue" if is_mv else ("readonly" if a.get("readOnly") else "settable")
            if wanted is not None:
                if not any(w in a["name"].lower() for w in wanted):
                    continue
            elif kind == "readonly" and not include_readonly:
                continue
            row: dict[str, Any] = {"name": a["name"], "kind": kind}
            if a.get("description") and a["description"] != a["name"]:
                row["description"] = a["description"]
            if is_mv:
                m = mv.get(a["name"], {})
                if m.get("format"):
                    row["format"] = m["format"]
                if m.get("enums"):
                    row["enums"] = m["enums"]
                if "default" in m:
                    row["default"] = _compact_default(m["default"])
            else:
                row["type"] = _type_str(t)
                if t.get("enums"):
                    row["enums"] = t["enums"]
                if a.get("default") not in (None, "", "kVoid"):
                    row["default"] = a["default"]
            rows.append(row)
        return rows

    def children(self, info: NodeInfo) -> list[dict[str, Any]]:
        out = []
        for c in info.raw.get("children") or []:
            row: dict[str, Any] = {"name": c["name"]}
            # a child the parent's own pass stopped at may still be crawled as a scope root of its own
            if c.get("skipped") or (c.get("not_crawled") and c.get("type_path") not in self.nodes):
                row["not_indexed"] = True
            else:
                ci = self.resolve(c["type_path"]) if "same_as" not in c else self.resolve(c["same_as"])
                if ci is not None:
                    row["multiplicity"] = ci.raw.get("multiplicity")
                    row["creatable"] = bool(ci.raw.get("add"))
            out.append(row)
        return out

    @staticmethod
    def operations(info: NodeInfo) -> list[dict[str, Any]]:
        ops: dict[str, dict[str, Any]] = {}
        for o in info.raw.get("operations") or []:
            if o.get("deprecated") or o.get("internal"):
                continue
            name = o["operation"]
            entry = ops.setdefault(name, {"name": name, "description": o.get("description") or "", "overloads": []})
            entry["overloads"].append([a["name"] for a in o.get("args") or []])
        return sorted(ops.values(), key=lambda e: e["name"])

    def describe(self, node_type: str, *, attributes: list[str] | None = None,
                 include: list[str] | None = None, include_readonly: bool = False) -> dict[str, Any] | None:
        info, paths = self.lookup(node_type)
        if info is None:
            return None
        include = include or ["attributes", "children"]
        out: dict[str, Any] = {
            "type": info.name,
            "typical_path": paths[0] if paths else info.canonical,
            "multiplicity": info.raw.get("multiplicity"),
            "creatable": bool(info.raw.get("add")),
            "description": info.raw.get("description"),
            "found_under": paths[:15],
            "schema_build": self.build,
        }
        if len(paths) > 15:
            out["found_under_total"] = len(paths)
        if "attributes" in include:
            out["attributes"] = self.attribute_rows(info, include_readonly=include_readonly, names=attributes)
        if "children" in include:
            out["children"] = self.children(info)
        if "operations" in include:
            out["operations"] = self.operations(info)
        return out

    # ------------------------------------------------------------------ search

    def search(self, query: str, under: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        tokens = [t for t in re.split(r"[\s_/.,-]+", query.lower()) if t]
        if not tokens:
            return []
        scored: dict[str, tuple[float, str]] = {}
        for name, paths in self.by_name.items():
            info, _ = self.lookup(name)
            if info is None:
                continue
            if under and not any(p.startswith(under.strip("/")) for p in paths):
                continue
            lname = name.lower()
            desc = (info.raw.get("description") or "").lower()
            score = 0.0
            for t in tokens:
                if lname == t:
                    score += 10
                elif t in lname:
                    score += 5
                elif t in desc:
                    score += 1
            if score == 0 or score < len(tokens):  # every token should contribute something
                continue
            score -= len(info.canonical) / 1000  # prefer shallower types on ties
            prev = scored.get(name)
            if prev is None or score > prev[0]:
                scored[name] = (score, info.canonical)
        ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])[:limit]
        out = []
        for name, (_, canonical) in ranked:
            info = self.resolve(canonical)
            _, paths = self.lookup(name)
            out.append({
                "type": name,
                "path": paths[0] if paths else canonical,
                "creatable": bool(info.raw.get("add")) if info else None,
                "multiplicity": info.raw.get("multiplicity") if info else None,
                "description": ((info.raw.get("description") or "")[:160]) if info else "",
                "occurrences": len(paths),
            })
        return out


def _path_preference(path: str) -> tuple[bool, bool, int]:
    """Order occurrences: NGPF topology first, the usual Ethernet stack next, then shortest."""
    return (not path.startswith("topology"), "/ethernet/" not in path and not path.endswith("/ethernet"), len(path))


def _type_str(t: dict[str, Any]) -> str:
    name = t.get("name", "?")
    if name == "list" and t.get("innerType"):
        return f"list[{_type_str(t['innerType'])}]"
    if name == "href" and t.get("hrefs"):
        targets = [h.rsplit("/ixnetwork/", 1)[-1] for h in t["hrefs"] if h != "null"]
        return f"ref({'|'.join(targets)})" if targets else "ref"
    return name


def _compact_default(d: dict[str, Any]) -> Any:
    p = d.get("pattern")
    if p == "singleValue":
        return d.get("value")
    if p == "counter":
        return {"start": d.get("start"), "step": d.get("step"), "direction": d.get("direction")}
    if p == "valueList":
        return {"list": d.get("values")}
    return {k: v for k, v in d.items() if k != "nest"}


def _build_key(build: str) -> tuple[int, ...]:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", build))


def _cache_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for d in (CACHE_DIR, USER_CACHE_DIR):  # user cache wins for the same build
        for p in d.glob("*.json.gz"):
            if ".partial" not in p.name and not p.name.endswith(".tmp"):
                files[p.name[: -len(".json.gz")]] = p
    return files


def available_builds() -> list[str]:
    return sorted(_cache_files(), key=_build_key)


def user_cache_path(build: str) -> Path:
    return USER_CACHE_DIR / f"{build}.json.gz"


def learned_path(build: str) -> Path:
    """Multivalue formats / enums learned from live objects of this build (see SchemaIndex.remember)."""
    return USER_CACHE_DIR / f"mv_meta_{build}.json"


def _read_learned(build: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(learned_path(build).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_learned(build: str, canonical: str, rows: dict[str, dict[str, Any]]) -> None:
    with _learn_lock:  # several targets on one build may learn at the same time
        data = _read_learned(build)
        data.setdefault(canonical, {}).update(rows)
        path = learned_path(build)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(path)


def _nearest(builds: list[str], build: str | None) -> str:
    if not build:
        return builds[-1]
    key = _build_key(build)
    return min(builds, key=lambda b: (abs(_build_key(b)[0] - key[0]) if key else 0, [-x for x in _build_key(b)]))


@functools.lru_cache(maxsize=8)
def get_index(build: str | None = None) -> tuple[SchemaIndex, bool]:
    """Return (index, exact_match). Without a cache for ``build`` the nearest cached build is
    used (the caller should warn, or crawl the target first). Multivalue metadata comes from
    the same build when captured, else from the nearest build that has it."""
    files = _cache_files()
    if not files:
        raise FileNotFoundError(f"no schema cache in {CACHE_DIR} or {USER_CACHE_DIR}")
    builds = sorted(files, key=_build_key)
    exact = build in files
    chosen = build if exact else _nearest(builds, build)
    with gzip.open(files[chosen], "rt", encoding="utf-8") as fh:
        nodes = json.load(fh)["nodes"]
    mv_builds = sorted((p.stem[len("mv_meta_"):] for p in DATA_DIR.glob("mv_meta_*.json")), key=_build_key)
    mv_build = chosen if chosen in mv_builds else (_nearest(mv_builds, chosen) if mv_builds else None)
    mv_meta = json.loads((DATA_DIR / f"mv_meta_{mv_build}.json").read_text()) if mv_build else {}
    learn_key = build or chosen  # what live objects of the target's own build taught earlier
    return SchemaIndex(nodes, mv_meta, chosen, mv_build, learned=_read_learned(learn_key), learn_key=learn_key), exact
