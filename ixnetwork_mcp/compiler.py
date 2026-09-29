"""Generic write engine: set attributes, add/delete objects, raw import, operations.

Why the split between REST verbs:
* plain attributes -> POST/PATCH: the server answers 400 on bad values/unknown attributes;
* multivalues      -> one ResourceManager.ImportConfig batch: never raises, applies partially
  and can switch a pattern even when rejecting the value -> validate against the object's
  *live* format/enums first, verify afterwards, restore the previous values on failure.
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import rest
from .errors import IXN_ERROR, NOT_FOUND, VALIDATION, IxToolError
from .refs import ConfigIndex, Node
from .schema.index import SchemaIndex
from .values import (ValueSpec, ValueSyntaxError, compile_value, mv_to_value, parse_value, same_value,
                     validate_value)

log = logging.getLogger("ixnetwork_mcp.compiler")

# attributes that must go through a dedicated tool
REDIRECT_ATTRS = {
    ("vport", "location"): "assign_ports",
    ("vport", "connectedTo"): "assign_ports",
    ("vport", "assignedTo"): "assign_ports",
}
# operations never run through run_operation
DENY_OPERATIONS = {
    "newconfig": "new_config", "loadconfig": "load_config", "saveconfig": "save_config",
    "import": "load_config", "loadtopology": None, "assignports": "assign_ports",
    "unassignports": "unassign_ports", "releaseport": "unassign_ports", "clearownership": None,
    "clearcardownershipbyid": None, "hwrebootcardbyids": None, "rebootvirtualchassis": None,
    "connectcardbyid": None, "disconnectcardbyid": None, "removealltclviews": None,
    "rebuildchassistopology": None, "refreshchassistopology": None, "rediscoverappliances": None,
    "collectlogs": None, "copyfile": None,
}
READ_PREFIXES = ("get", "fetch", "check")


# ---------------------------------------------------------------------------- import

@dataclass
class ImportOutcome:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify_import_result(msgs: list[Any] | None) -> ImportOutcome:
    out = ImportOutcome()
    for m in msgs or []:
        text = " ".join(str(m).split())
        if "kError" in text:
            out.errors.append(text)
        elif "invalidJsonProperty" in text:
            out.errors.append(f"not accepted (unknown attribute or child object): {text}")
        elif text:
            out.warnings.append(text)
    return out


def run_import(ixn: Any, entries: list[dict[str, Any]]) -> ImportOutcome:
    if not entries:
        return ImportOutcome()
    return classify_import_result(ixn.ResourceManager.ImportConfig(json.dumps(entries), False))


def href_of(created: Any) -> str:
    if isinstance(created, dict):
        for link in created.get("links") or []:
            if link.get("rel") == "self":
                return link["href"].rstrip("/")
        if created.get("href"):
            return created["href"].rstrip("/")
    raise IxToolError(IXN_ERROR, f"could not read the href of the created object: {str(created)[:200]}")


def on_the_fly_warning(ixn: Any) -> str | None:
    state = rest.get(ixn, "globals/topology?includes=applyOnTheFlyState").get("applyOnTheFlyState")
    if state == "allowed":
        return ("protocols are running: the change is pending until applied on the fly "
                "(control_protocols(action='apply_changes') once available, or 'Apply changes' in the GUI)")
    return None


# ---------------------------------------------------------------------------- plain attributes

def _suggest(name: str, choices: list[str]) -> list[str] | None:
    return difflib.get_close_matches(name, choices, n=5, cutoff=0.5) or None


def convert_plain(meta: dict[str, Any], attr: str, raw: Any, index: ConfigIndex) -> tuple[Any, str | None]:
    """Convert an agent value for a plain (non-multivalue) attribute; returns (value, error)."""
    t = meta.get("type") or {}
    name = t.get("name")
    try:
        if name == "number":
            if isinstance(raw, bool):
                raise ValueError
            n = float(raw)
            return (int(n) if n.is_integer() else n), None
        if name == "bool":
            if isinstance(raw, bool):
                return raw, None
            if str(raw).lower() in ("true", "false"):
                return str(raw).lower() == "true", None
            raise ValueError
        if name == "enum":
            enums = t.get("enums") or []
            if str(raw) not in enums:
                return None, f"{attr}: '{raw}' is not one of {enums}"
            return str(raw), None
        if name == "href":
            return index.resolve(str(raw)).href, None
        if name == "list" and (t.get("innerType") or {}).get("name") == "href":
            items = raw if isinstance(raw, list) else [raw]
            return [index.resolve(str(x)).href for x in items], None
        if name == "string":
            return str(raw), None
    except IxToolError as exc:
        return None, f"{attr}: {exc.message}"
    except (TypeError, ValueError):
        return None, f"{attr}: '{raw}' is not a valid {name}"
    return raw, None


# ---------------------------------------------------------------------------- set_attributes

@dataclass
class _MvChange:
    attr: str
    spec: ValueSpec
    fmt: str | None
    before: Any          # full form incl. disabled nests, used to restore
    before_shown: Any    # what get_node would show
    nest_count: int


def _plan_attributes(node: Node, live: dict[str, Any], meta: dict[str, dict[str, Any]],
                     values: dict[str, Any], index: ConfigIndex, *, creating: bool = False
                     ) -> tuple[dict[str, Any], list[_MvChange], dict[str, Any], list[str]]:
    patch: dict[str, Any] = {}
    mvs: list[_MvChange] = []
    before: dict[str, Any] = {}
    errors: list[str] = []
    for attr, raw in values.items():
        if (node.type, attr) in REDIRECT_ATTRS:
            errors.append(f"{attr}: use the {REDIRECT_ATTRS[(node.type, attr)]} tool to change this")
            continue
        m = meta.get(attr)
        cur = live.get(attr)
        if m is None and cur is None:
            errors.append(f"{node.type} has no attribute '{attr}'"
                          + (f" (did you mean {_suggest(attr, sorted(meta or live))}?)" if _suggest(attr, sorted(meta or live)) else ""))
            continue
        if m and m.get("deprecated"):
            errors.append(f"{attr} is deprecated on this IxNetwork build; see describe_node_type('{node.type}')")
            continue
        if rest.is_multivalue(cur):
            try:
                spec = parse_value(raw, attr)
            except ValueSyntaxError as exc:
                errors.append(str(exc))
                continue
            patterns = [p for p in cur.get("availablePatterns") or [] if p not in ("shared", "subset")]
            errors += validate_value(spec, attr, cur.get("format"), cur.get("enums") or None, patterns or None)
            mvs.append(_MvChange(attr, spec, cur.get("format"), mv_to_value(cur, show_disabled_nest=True),
                                 mv_to_value(cur), len(cur.get("nest") or [])))
            continue
        kind = (m or {}).get("kind")
        if kind == "readonly" or (m is None and not creating):
            errors.append(f"{attr} is read-only")
            continue
        value, err = convert_plain(m or {}, attr, raw, index)
        if err:
            errors.append(err)
            continue
        patch[attr] = value
        before[attr] = cur
    return patch, mvs, before, errors


def _restore(ixn: Any, node: Node, patch_before: dict[str, Any], mvs: list[_MvChange]) -> list[str]:
    """Best-effort rollback; returns what could not be restored."""
    problems: list[str] = []
    if patch_before:
        try:
            ixn._connection._update(node.href, {k: v for k, v in patch_before.items() if v is not None})
        except Exception as exc:  # noqa: BLE001
            problems.append(f"plain attributes: {exc}")
    entries: list[dict[str, Any]] = []
    for c in mvs:
        try:
            entries += compile_value(node.xpath, c.attr, parse_value(c.before, c.attr), c.nest_count)
        except (ValueSyntaxError, TypeError) as exc:
            problems.append(f"{c.attr}: previous value not restorable ({exc})")
    outcome = run_import(ixn, entries)
    problems += outcome.errors
    return problems


def set_attributes(ixn: Any, schema: SchemaIndex, ref: str, values: dict[str, Any],
                   dry_run: bool = False) -> dict[str, Any]:
    if not values:
        raise IxToolError(VALIDATION, "no values given")
    index = ConfigIndex.build(ixn, props=["name"])
    node = index.resolve(ref)
    live = rest.select(ixn, node.href, properties=["*"], inlines=rest.MV_INLINES)[0]
    meta = schema.attribute_meta(node.type_path)
    patch, mvs, before, errors = _plan_attributes(node, live, meta, values, index)
    if errors:
        raise IxToolError(VALIDATION, f"{len(errors)} problem(s) with the values for {node.name or node.xpath}",
                          errors=errors, hint="describe_node_type shows attribute kinds, formats and enums.")
    entries: list[dict[str, Any]] = []
    for c in mvs:
        try:
            entries += compile_value(node.xpath, c.attr, c.spec, c.nest_count)
        except ValueSyntaxError as exc:
            raise IxToolError(VALIDATION, str(exc)) from exc
    target = {"name": node.name, "type": node.type, "xpath": node.xpath}
    if dry_run:
        return {"ref": target, "dry_run": True, "patch": patch, "import_entries": entries,
                "before": {**before, **{c.attr: c.before_shown for c in mvs}}}

    if patch:
        ixn._connection._update(node.href, patch)  # raises (400) on bad values -> nothing else applied yet
    outcome = run_import(ixn, entries)
    after = rest.select(ixn, node.href, properties=["*"], inlines=rest.MV_INLINES)[0]
    mismatched = [f"{k}: requested {v!r}, server has {after.get(k)!r}" for k, v in patch.items()
                  if after.get(k) != v and not (isinstance(v, list) and sorted(v) == sorted(after.get(k) or []))]
    for c in mvs:
        got = mv_to_value(after.get(c.attr) or {})
        if not same_value(c.spec, got, c.fmt):
            mismatched.append(f"{c.attr}: requested {values[c.attr]!r}, server has {got!r}")
    if outcome.errors or mismatched:
        problems = _restore(ixn, node, before, mvs)
        raise IxToolError(IXN_ERROR, f"IxNetwork did not accept the change to {node.name or node.xpath}",
                          errors=outcome.errors or None, mismatched=mismatched or None,
                          rolled_back=not problems, rollback_problems=problems or None)
    changed = {k: {"before": before[k], "after": after.get(k)} for k in patch}
    changed.update({c.attr: {"before": c.before_shown, "after": mv_to_value(after.get(c.attr) or {})} for c in mvs})
    warnings = outcome.warnings + [w for w in [on_the_fly_warning(ixn)] if w]
    return {"ref": target, "changed": changed, "warnings": warnings}


# ---------------------------------------------------------------------------- add_node

def add_node(ixn: Any, schema: SchemaIndex, parent_ref: str | None, node_type: str,
             attributes: dict[str, Any] | None, dry_run: bool = False) -> dict[str, Any]:
    attributes = dict(attributes or {})
    index = ConfigIndex.build(ixn, props=["name", "ports"])
    root = rest.root(ixn)
    parent = index.resolve(parent_ref) if parent_ref and parent_ref.strip() not in ("", "/") else None
    parent_tp = parent.type_path if parent else ""
    child_tp = schema.child_type_path(parent_tp, node_type)
    if child_tp is None:
        creatable = [c["name"] for c in (schema.children(schema.resolve(parent_tp)) if parent else
                                         [{"name": c["name"], "creatable": True} for c in
                                          (schema.nodes.get("") or {}).get("children", [])])
                     if c.get("creatable")]
        raise IxToolError(VALIDATION, f"'{node_type}' cannot be added under {parent.type if parent else 'the root'}",
                          candidates=_suggest(node_type, creatable) or creatable[:30])
    info = schema.resolve(child_tp)
    if info is None or not info.raw.get("add"):
        raise IxToolError(VALIDATION, f"'{node_type}' objects are created automatically by IxNetwork; "
                                      "change the existing one with set_attributes")
    name = attributes.get("name")
    if name and index.by_name.get(str(name)):
        raise IxToolError(VALIDATION, f"an object named '{name}' already exists (names must be unique)")
    if node_type == "topology":
        ports = attributes.get("ports") or attributes.get("vports") or []
        for p in ports if isinstance(ports, list) else [ports]:
            vp = index.resolve(str(p), types=["vport"])
            users = [t.name for t in index.nodes if t.type == "topology" and
                     vp.href in [h.rstrip("/") for h in t.props.get("ports") or []]]
            if users:
                raise IxToolError(VALIDATION, f"port '{vp.name}' is already used by topology '{users[0]}' "
                                              "(a port can belong to only one topology)")
    meta = schema.attribute_meta(child_tp)
    plain_in = {k: v for k, v in attributes.items() if meta.get(k, {}).get("kind") != "multivalue"}
    mv_in = {k: v for k, v in attributes.items() if meta.get(k, {}).get("kind") == "multivalue"}
    placeholder = Node(href="", xpath=f"{parent.xpath if parent else ''}/{node_type}[new]", type=node_type,
                       name=str(name) if name else None)
    body, _, _, errors = _plan_attributes(placeholder, {}, meta, plain_in, index, creating=True)
    for attr, raw in mv_in.items():  # validate against captured metadata; exact check after creation
        try:
            spec = parse_value(raw, attr)
            m = meta[attr].get("mv") or {}
            errors += validate_value(spec, attr, m.get("format"), m.get("enums"), m.get("availablePatterns"))
        except ValueSyntaxError as exc:
            errors.append(str(exc))
    if errors:
        raise IxToolError(VALIDATION, f"{len(errors)} problem(s) with the attributes for the new {node_type}",
                          errors=errors, hint=f"describe_node_type('{child_tp}') lists the attributes.")
    url = f"{parent.href if parent else root}/{node_type}"
    if dry_run:
        return {"dry_run": True, "create": {"type": node_type, "parent": parent.xpath if parent else "/",
                                            "body": body}, "then_set": mv_in}
    try:
        created = ixn._connection._create(url, body)
    except Exception:
        _cleanup_failed_create(ixn, url, body.get("name"))
        raise
    href = href_of(created)
    try:
        node_sel = rest.select(ixn, href, properties=["name"])[0]
        new = Node(href=href, xpath=node_sel["xpath"], type=node_type, name=node_sel.get("name"))
        result: dict[str, Any] = {"created": {"name": new.name, "type": node_type, "xpath": new.xpath, "href": href}}
        if mv_in:
            set_result = set_attributes(ixn, schema, href, mv_in)
            result["values"] = {k: v["after"] for k, v in set_result["changed"].items()}
    except Exception:
        try:
            ixn._connection._delete(href)
        except Exception as cleanup_exc:  # noqa: BLE001
            log.warning("rollback of %s failed: %s", href, cleanup_exc)
        raise
    kids = rest.select(ixn, href, properties=["name"], children=[rest.child(r"^(?!(port|tag|learnedInfo)$)[A-Za-z0-9]+$", ["name"])])
    auto = [{"name": k.get("name"), "type": rest.node_type(k["xpath"]), "xpath": k["xpath"]}
            for k in rest.child_nodes(kids[0])]
    if auto:
        result["auto_created_children"] = auto
    warn = on_the_fly_warning(ixn)
    if warn:
        result["warnings"] = [warn]
    return result


def _cleanup_failed_create(ixn: Any, url: str, name: Any) -> None:
    """A rejected POST can still leave an empty object behind (e.g. a topology whose port was
    taken); delete it if it exists under the expected name."""
    if not name:
        return
    try:
        for obj in ixn._connection._read(f"{url}?includes=name") or []:
            if obj.get("name") == name:
                ixn._connection._delete(f"{url}/{obj['id']}")
                log.info("removed leftover %s/%s after failed create", url, obj["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("cleanup after failed create at %s: %s", url, exc)


# ---------------------------------------------------------------------------- delete

def delete_nodes(ixn: Any, schema: SchemaIndex, refs: list[str]) -> dict[str, Any]:
    index = ConfigIndex.build(ixn, props=["name"])
    nodes = [index.resolve(r) for r in refs]
    problems = []
    for n in nodes:
        info = schema.resolve(n.type_path)
        if info is not None and not info.raw.get("remove"):
            problems.append(f"{n.name or n.xpath}: {n.type} objects cannot be deleted (created automatically)")
    if problems:
        raise IxToolError(VALIDATION, "some objects cannot be deleted", errors=problems)
    # deleting a parent removes its subtree; skip descendants of other targets
    xps = {n.xpath for n in nodes}
    todo = [n for n in nodes if not any(n.xpath.startswith(x + "/") for x in xps if x != n.xpath)]
    deleted, skipped = [], [n.xpath for n in nodes if n not in todo]
    for n in sorted(todo, key=lambda n: -n.xpath.count("/")):
        ixn._connection._delete(n.href)
        deleted.append({"name": n.name, "type": n.type, "xpath": n.xpath})
    out: dict[str, Any] = {"deleted": deleted}
    if skipped:
        out["included_in_deleted_parent"] = skipped
    return out


# ---------------------------------------------------------------------------- raw import

def import_fragment(ixn: Any, fragment: Any, dry_run: bool = True) -> dict[str, Any]:
    entries = fragment if isinstance(fragment, list) else [fragment]
    bad = [i for i, e in enumerate(entries) if not isinstance(e, dict) or not str(e.get("xpath", "")).startswith("/")]
    if bad:
        raise IxToolError(VALIDATION, f"entries {bad[:10]} are not objects with an 'xpath' starting with '/'")
    index = ConfigIndex.build(ixn, props=["name"])
    summary = []
    for e in entries:
        xp = e["xpath"]
        owner = xp
        if xp.startswith("/multivalue[@source = '"):
            owner = xp.split("'")[1].rsplit(" ", 1)[0]
        exists = owner in index.by_xpath or owner in ("/",)
        summary.append({"xpath": xp, "action": "modify" if exists else "create"})
    if dry_run:
        return {"dry_run": True, "entries": len(entries), "plan": summary[:200]}
    outcome = run_import(ixn, entries)
    if outcome.errors:
        raise IxToolError(IXN_ERROR, f"ImportConfig reported {len(outcome.errors)} error(s); "
                                     "IxNetwork may have applied the valid entries (no rollback for raw imports)",
                          errors=outcome.errors[:20])
    return {"applied": len(entries), "warnings": outcome.warnings}


# ---------------------------------------------------------------------------- operations

def check_denied(operation: str) -> None:
    key = operation.lower()
    if key in DENY_OPERATIONS:
        alt = DENY_OPERATIONS[key]
        raise IxToolError(VALIDATION, f"operation '{operation}' is not available through run_operation"
                          + (f"; use the {alt} tool" if alt else ""))


def resolve_operation(schema: SchemaIndex, type_path: str, operation: str) -> tuple[str, list[dict[str, Any]]]:
    info = schema.resolve(type_path) if type_path else None
    raw_ops = (info.raw if info else schema.nodes.get("", {})).get("operations") or []
    by_lower: dict[str, list[dict[str, Any]]] = {}
    for o in raw_ops:
        by_lower.setdefault(o["operation"].lower(), []).append(o)
    key = operation.lower()
    check_denied(operation)
    if key not in by_lower:
        raise IxToolError(NOT_FOUND, f"no operation '{operation}' on {type_path or 'the root'}",
                          candidates=_suggest(operation, sorted({o["operation"] for o in raw_ops})) or
                          sorted({o["operation"] for o in raw_ops})[:30])
    overloads = by_lower[key]
    return overloads[0]["operation"], overloads


def is_read_operation(name: str) -> bool:
    return name.lower().startswith(READ_PREFIXES)


def run_operation(ixn: Any, schema: SchemaIndex, ref: str | None, operation: str,
                  args: dict[str, Any] | None) -> dict[str, Any]:
    root = rest.root(ixn)
    if ref and ref.strip() not in ("", "/"):
        node = ConfigIndex.build(ixn, props=["name"]).resolve(ref)
        href, type_path = node.href, node.type_path
    else:
        node, href, type_path = None, root, ""
    name, overloads = resolve_operation(schema, type_path, operation)
    payload: dict[str, Any] = {}
    first = (overloads[0].get("args") or [{}])[0] if overloads[0].get("args") else None
    if first is not None and first.get("name") in ("arg1", "targets") and node is not None:
        is_list = (first.get("type") or {}).get("name") == "list"
        payload["arg1"] = [href] if is_list else href
    payload.update(args or {})
    result = ixn._connection._execute(f"{href}/operations/{name}", payload)
    return {"operation": name, "target": node.xpath if node else "/", "result": result}
