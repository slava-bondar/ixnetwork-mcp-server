"""Explicit configuration saves, restore and drift detection.

A save is a directory ``<config_dir>/<name>/`` on the MCP host (never in the repo):
  config.ixncfg   SaveConfig on the VM -> download -> VM copy deleted  (exact restore)
  config.json     scoped ExportConfig of locations, vports (incl. L1), topologies and traffic-item
                  definitions without generated flow groups (portable, diffable;
                  ImportConfig(json, True) restores it with ports link-up)
  manifest.json   target, build, timestamp, vport -> location map, sha256s, note
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import rest
from .errors import NOT_FOUND, VALIDATION, IxToolError

SAVE_SCOPE = [
    ["/locations/descendant-or-self::*"],
    ["/vport/descendant-or-self::*"],
    ["/topology/descendant-or-self::*"],
    ["/traffic/trafficItem", "/traffic/trafficItem/endpointSet",
     "/traffic/trafficItem/configElement/descendant-or-self::*",
     "/traffic/trafficItem/tracking/descendant-or-self::*"],
]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_dir(base: Path, name: str | None, path: str | None) -> tuple[str, Path]:
    name = name or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if not _NAME.match(name):
        raise IxToolError(VALIDATION, f"invalid save name '{name}'",
                          hint="Use letters, digits, '.', '_' or '-' (max 100 characters).")
    root = Path(path).expanduser() if path else base
    return name, root / name


def export_json(ixn: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {"xpath": "/"}
    for xpaths in SAVE_SCOPE:
        part = rest.export(ixn, xpaths)
        merged.update({k: v for k, v in part.items() if k != "xpath"})
    return merged


def save(ixn: Any, *, target: str, build: str | None, dest: Path, name: str, formats: list[str],
         note: str | None, overwrite: bool) -> dict[str, Any]:
    from ixnetwork_restpy import Files

    bad = [f for f in formats if f not in ("ixncfg", "json")]
    if bad or not formats:
        raise IxToolError(VALIDATION, f"formats must be a non-empty subset of ['ixncfg', 'json'], got {formats}")
    if dest.exists() and not overwrite:
        raise IxToolError(VALIDATION, f"a saved config '{name}' already exists at {dest}",
                          hint="Choose another name or pass overwrite=true.")
    dest.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    if "ixncfg" in formats:
        remote = f"mcp-save-{name}.ixncfg"
        local = dest / "config.ixncfg"
        ixn.SaveConfig(Files(remote, local_file=False))
        try:
            ixn._connection._get_file(rest.root(ixn), remote, local_filename=str(local))
        finally:
            ixn._connection._delete_file(rest.root(ixn), remote)
        files["config.ixncfg"] = {"bytes": local.stat().st_size, "sha256": _sha256(local)}
    if "json" in formats:
        local = dest / "config.json"
        local.write_text(json.dumps(export_json(ixn), separators=(",", ":")))
        files["config.json"] = {"bytes": local.stat().st_size, "sha256": _sha256(local)}
    vports = rest.get(ixn, "vport?includes=name,location")
    counts = {
        "topologies": len(rest.get(ixn, "topology?includes=name")),
        "traffic_items": len(rest.get(ixn, "traffic/trafficItem?includes=name")),
        "vports": len(vports),
    }
    manifest = {
        "name": name,
        "target": target,
        "build": build,
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "ports": {v["name"]: v.get("location") or None for v in vports},
        "counts": counts,
        "files": files,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"name": name, "path": str(dest), **{k: manifest[k] for k in ("build", "saved_at", "counts", "files")}}


def list_saved(root: Path) -> list[dict[str, Any]]:
    rows = []
    if not root.exists():
        return rows
    for mf in sorted(root.glob("*/manifest.json")):
        try:
            m = json.loads(mf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({"name": m.get("name", mf.parent.name), "path": str(mf.parent), "saved_at": m.get("saved_at"),
                     "build": m.get("build"), "target": m.get("target"), "note": m.get("note"),
                     "formats": sorted(k.split(".")[-1] for k in (m.get("files") or {})),
                     "counts": m.get("counts"), "ports": m.get("ports")})
    return sorted(rows, key=lambda r: r.get("saved_at") or "", reverse=True)


# ---------------------------------------------------------------------------- load / drift / new

def resolve_saved(base: Path, name_or_path: str) -> Path:
    p = Path(name_or_path).expanduser()
    for cand in (p, base / name_or_path):
        if (cand / "manifest.json").is_file():
            return cand
    names = [r["name"] for r in list_saved(base)]
    raise IxToolError(NOT_FOUND, f"no saved config '{name_or_path}' (looked in {base})", candidates=names[:20] or None,
                      hint="list_saved_configs shows what is available.")


def _apply_port_map_json(doc: dict[str, Any], port_map: dict[str, str]) -> list[str]:
    applied = []
    for vp in doc.get("vport") or []:
        new = port_map.get(vp.get("name")) or port_map.get(vp.get("location"))
        if new:
            applied.append(f"{vp.get('name')}: {vp.get('location')} -> {new}")
            vp["location"] = new
    hosts = {loc.get("hostname") for loc in doc.get("locations") or []}
    for new in port_map.values():
        host = new.split("/")[0]
        if host not in hosts:
            doc.setdefault("locations", []).append({"xpath": f"/locations[{len(doc.get('locations') or []) + 1}]",
                                                    "hostname": host})
            hosts.add(host)
    return applied


def _wait_all_ports(ixn: Any, wait_s: float, expected: dict[str, str | None]) -> list[dict[str, Any]]:
    """Wait until every vport that has a location in the save (``expected``) is link-up.
    Right after LoadConfig the vports exist without location for a while, so "all located
    vports are up" would be vacuously true."""
    import time as _t

    want = {name for name, loc in expected.items() if loc}
    deadline = _t.monotonic() + wait_s
    while True:
        vps = rest.get(ixn, "vport?includes=name,location,connectionState")
        up = {v["name"] for v in vps if v.get("connectionState") == "connectedLinkUp"}
        done = want <= up if want else all(v.get("connectionState") == "connectedLinkUp" for v in vps if v.get("location"))
        if done or _t.monotonic() >= deadline:
            return [{"vport": v["name"], "location": v.get("location") or None, "state": v.get("connectionState")}
                    for v in vps]
        _t.sleep(2)


def load(ixn: Any, saved: Path, method: str, port_map: dict[str, str] | None, wait_s: float) -> dict[str, Any]:
    from ixnetwork_restpy import Files

    from .compiler import classify_import_result

    manifest = json.loads((saved / "manifest.json").read_text())
    warnings: list[str] = []
    applied_map: list[str] = []
    if method == "ixncfg":
        f = saved / "config.ixncfg"
        if not f.is_file():
            raise IxToolError(VALIDATION, f"{saved.name} has no config.ixncfg; use method='json'")
        ixn.LoadConfig(Files(str(f), local_file=True))
        try:  # the upload stays in the VM's file area otherwise
            ixn._connection._delete_file(rest.root(ixn), f.name)
        except Exception:  # noqa: BLE001
            warnings.append("could not remove the uploaded file from the VM")
        if port_map:
            vps = {v["name"]: v for v in rest.get(ixn, "vport?includes=name,location")}
            for key, new in port_map.items():
                v = vps.get(key) or next((x for x in vps.values() if x.get("location") == key), None)
                if v is None:
                    warnings.append(f"port_map: no vport '{key}' in the loaded config")
                    continue
                ixn._connection._update(f"{rest.root(ixn)}/vport/{v['id']}", {"location": new})
                applied_map.append(f"{v['name']}: {v.get('location')} -> {new}")
    elif method == "json":
        f = saved / "config.json"
        if not f.is_file():
            raise IxToolError(VALIDATION, f"{saved.name} has no config.json; use method='ixncfg'")
        doc = json.loads(f.read_text())
        if port_map:
            applied_map = _apply_port_map_json(doc, port_map)
        ixn.NewConfig()
        outcome = classify_import_result(ixn.ResourceManager.ImportConfig(json.dumps(doc), True))
        if outcome.errors:
            warnings += [f"import: {e}" for e in outcome.errors[:20]]
    else:
        raise IxToolError(VALIDATION, "method must be 'ixncfg' or 'json'")
    items = rest.get(ixn, "traffic/trafficItem?includes=name")
    if items:
        hrefs = [f"{rest.root(ixn)}/traffic/trafficItem/{i['id']}" for i in items]
        try:
            ixn._connection._execute(f"{rest.root(ixn)}/traffic/trafficItem/operations/generate", {"arg1": hrefs})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"traffic regeneration failed: {exc}")
    expected = dict(manifest.get("ports") or {})
    for key, new in (port_map or {}).items():  # moved ports are expected at their new location
        for name, loc in list(expected.items()):
            if key in (name, loc):
                expected[name] = new
    ports = _wait_all_ports(ixn, wait_s, expected)
    down = [p for p in ports if p["location"] and p["state"] != "connectedLinkUp"]
    if down:
        warnings.append(f"{len(down)} port(s) not link-up: " + ", ".join(f"{p['vport']}={p['state']}" for p in down[:8]))
    result: dict[str, Any] = {"loaded": manifest.get("name", saved.name), "method": method,
                              "saved_at": manifest.get("saved_at"), "saved_build": manifest.get("build"),
                              "ports": ports, "port_map_applied": applied_map or None,
                              "traffic_items_regenerated": len(items)}
    if (saved / "config.json").is_file():
        result["drift_vs_save"] = drift(json.loads((saved / "config.json").read_text()), export_json(ixn),
                                        limit=20)["summary"]
    result["warnings"] = warnings
    return result


_MV_PATTERNS = ("singleValue", "counter", "valueList", "string", "alternate", "repeatableRandomRange",
                "random", "custom", "customDistributed", "repeatableRandom")


_SEP = "#"  # object key / multivalue attribute separator (names may contain spaces)


def _flatten(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Export JSON -> {stable key: {attr: value}}. Nodes are keyed by type:name paths (stable
    across deletions, unlike positional xpaths); multivalues become '<owner key>#<attr>'."""
    names: dict[str, str] = {}
    nodes: list[tuple[str, dict[str, Any]]] = []
    mvs: list[tuple[str, str, dict[str, Any]]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            xp = o.get("xpath")
            if isinstance(xp, str) and xp.startswith("/multivalue[@source = '"):
                src, attr = xp.split("'")[1].rsplit(" ", 1)
                mvs.append((src, attr, o))
                return
            if isinstance(xp, str) and xp != "/":
                nodes.append((xp, o))
                if isinstance(o.get("name"), str):
                    names[xp] = o["name"]
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)

    def key(xpath: str) -> str:
        parts, cur, out = xpath.strip("/").split("/"), "", []
        for seg in parts:
            cur = f"{cur}/{seg}"
            t = seg.split("[")[0]
            out.append(f"{t}:{names[cur]}" if cur in names else seg)
        return "/" + "/".join(out)

    flat: dict[str, dict[str, Any]] = {}
    for xp, node in nodes:
        attrs = {}
        for k, v in node.items():
            if k == "xpath" or isinstance(v, dict):
                continue
            if isinstance(v, list):
                if v and not isinstance(v[0], (dict, list)):
                    attrs[k] = [key(x) if isinstance(x, str) and x.startswith("/") else x for x in v]
                continue
            attrs[k] = v
        flat[key(xp)] = attrs
    for src, attr, mv in mvs:
        pat = next((p for p in _MV_PATTERNS if isinstance(mv.get(p), dict)), None)
        val: dict[str, Any] = {"pattern": pat}
        if pat:
            val.update({k: v for k, v in mv[pat].items() if k != "xpath"})
        nests = [{"enabled": n.get("enabled"), "step": n.get("step")} for n in mv.get("nest") or []]
        if any(n["enabled"] for n in nests):
            val["nest"] = nests
        flat[f"{key(src)}{_SEP}{attr}"] = val
    return flat


def drift(saved: dict[str, Any], live: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    a, b = _flatten(saved), _flatten(live)
    added = sorted(k for k in b if k not in a and _SEP not in k)
    removed = sorted(k for k in a if k not in b and _SEP not in k)
    gone_or_new = added + removed

    def inside(owner: str) -> bool:  # values of objects that were added/removed are not "changes"
        return any(owner == k or owner.startswith(k + "/") for k in gone_or_new)

    changed = []
    for k in sorted(set(a) | set(b)):
        if _SEP in k:
            owner, attr = k.split(_SEP, 1)
            if inside(owner) or a.get(k) == b.get(k):
                continue
            changed.append({"object": owner, "attribute": attr,
                            "saved": a.get(k, "(default)"), "live": b.get(k, "(default)")})
        elif k in a and k in b and a[k] != b[k]:
            for attr in sorted(set(a[k]) & set(b[k])):
                if a[k][attr] != b[k][attr]:
                    changed.append({"object": k, "attribute": attr, "saved": a[k][attr], "live": b[k][attr]})
    summary = {"added": len(added), "removed": len(removed), "changed": len(changed),
               "in_sync": not (added or removed or changed)}

    def top(keys: list[str]) -> list[str]:  # an added/removed object implies its children
        return [k for k in keys if not any(k.startswith(o + "/") for o in keys if o != k)]

    added_top, removed_top = top(added), top(removed)
    return {"summary": summary, "added": added_top[:limit], "removed": removed_top[:limit],
            "changed": changed[:limit],
            "truncated": any(len(x) > limit for x in (added_top, removed_top, changed)) or None}
