"""apply_config engine: curated specs -> anchor-then-import.

Per *unit* (a new topology, or one object added under an existing parent):
  1. validate the whole plan offline (placement, auto-object prerequisites, names, ports,
     attribute kinds, formats/enums)                          -> nothing is written on error
  2. POST the unit's root object (the anchor) and read its positional xpath
  3. compile the rest of the subtree into ONE ImportConfig batch relative to the anchor
  4. verify every planned object exists and every value landed (ImportConfig drops silently)
  5. on failure delete every anchor created by this call (rollback_on_error)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from . import compiler, rest
from .errors import IXN_ERROR, VALIDATION, IxToolError, map_exception
from .refs import ConfigIndex, Node
from .schema.index import SchemaIndex
from .specs import ConfigSpec, PlanError, PlanNode, extension_plans, topology_plan
from .values import ValueSyntaxError, compile_value, mv_to_value, parse_value, same_value, validate_value

log = logging.getLogger("ixnetwork_mcp.builder")

ROUTER_DATA_PROTOCOLS = {"bgpIpv4Peer", "bgpIpv6Peer", "ospfv2", "ospfv3"}
REQUIREMENTS = {  # auto node type -> protocols that make IxNetwork create it
    "routerData": ROUTER_DATA_PROTOCOLS,
    "ospfv2Router": {"ospfv2"},
    "ospfv3Router": {"ospfv3"},
    "isisL3Router": {"isisL3"},
    "bgpIPRouteProperty": {"bgpIpv4Peer"},
    "bgpV6IPRouteProperty": {"bgpIpv6Peer"},
    "ospfRouteProperty": {"ospfv2"},
    "ospfv3RouteProperty": {"ospfv3"},
    "isisL3RouteProperty": {"isisL3"},
}
CONTAINERS = ("deviceGroup", "networkGroup")
# semantic rules IxNetwork enforces at commit time (not expressed in the schema)
EXCLUSIVE_SIBLINGS = [({"igmpHost", "igmpQuerier"}, "IGMP host and IGMP querier cannot share the same IPv4 stack"),
                      ({"mldHost", "mldQuerier"}, "MLD host and MLD querier cannot share the same IPv6 stack")]
MAX_DRY_RUN_ENTRIES = 300


@dataclass
class Unit:
    label: str
    plan: PlanNode
    parent: Node | None = None                    # existing parent; None = root
    ports: list[str] = field(default_factory=list)  # topology units: vport hrefs


@dataclass
class _Checked:
    """Per plan node, after validation: typed attributes ready to compile."""
    node: PlanNode
    type_path: str
    plain: dict[str, Any]
    mv: dict[str, Any]                     # attr -> ValueSpec
    fmt: dict[str, str | None]
    has_nest: dict[str, bool]
    children: list["_Checked"]


# ---------------------------------------------------------------------------- helpers

def _protocols_in(plan: PlanNode) -> set[str]:
    """Protocol types in a device group's own stack (not in chained groups)."""
    found: set[str] = set()

    def walk(n: PlanNode) -> None:
        for c in n.children:
            if c.type in CONTAINERS:
                continue
            found.add(c.type)
            walk(c)

    walk(plan)
    return found


def _existing_protocols(index: ConfigIndex, dg: Node) -> set[str]:
    stop = [n.xpath for n in index.subtree(dg) if n.type in CONTAINERS]
    return {n.type for n in index.subtree(dg) if not any(n.xpath.startswith(s + "/") or n.xpath == s for s in stop)}


def _nearest_device_group(index: ConfigIndex, node: Node) -> Node | None:
    chain = [n for n in index.nodes if node.xpath == n.xpath or node.xpath.startswith(n.xpath + "/")]
    dgs = [n for n in chain if n.type == "deviceGroup"]
    return max(dgs, key=lambda n: len(n.xpath)) if dgs else None


def nest_levels(xpath: str) -> int:
    """Nest depth of multivalues owned at ``xpath``: 1 (port step) + number of device /
    network groups above the owning container (verified: 1 top DG, 2 inside a network group,
    3 for a device group chained behind a network group)."""
    segs = [s.split("[")[0] for s in xpath.strip("/").split("/")]
    idx = max((i for i, s in enumerate(segs) if s in CONTAINERS), default=None)
    if idx is None:
        return 0
    return 1 + sum(1 for s in segs[:idx] if s in CONTAINERS)


# ---------------------------------------------------------------------------- validation

class _Validator:
    def __init__(self, schema: SchemaIndex, index: ConfigIndex):
        self.schema = schema
        self.index = index
        self.errors: list[str] = []
        self.names: dict[str, str] = {}   # name -> label
        self.ports: dict[str, str] = {}   # vport href -> label

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def check_name(self, name: Any, label: str) -> None:
        if not name:
            return
        name = str(name)
        if self.index.by_name.get(name):
            self.err(f"{label}.name: an object named '{name}' already exists (names must be unique)")
        elif name in self.names:
            self.err(f"{label}.name: '{name}' is also used by {self.names[name]}")
        else:
            self.names[name] = label

    def check_ports(self, unit: Unit, port_refs: list[str]) -> None:
        for p in port_refs:
            try:
                vp = self.index.resolve(p, types=["vport"])
            except IxToolError as exc:
                self.err(f"{unit.label}.ports: {exc.message}")
                continue
            users = [t.name for t in self.index.nodes if t.type == "topology" and
                     vp.href in [h.rstrip("/") for h in t.props.get("ports") or []]]
            if users:
                self.err(f"{unit.label}.ports: '{vp.name}' is already used by topology '{users[0]}'")
            elif vp.href in self.ports:
                self.err(f"{unit.label}.ports: '{vp.name}' is also used by {self.ports[vp.href]}")
            else:
                self.ports[vp.href] = unit.label
                unit.ports.append(vp.href)

    def check_exclusive(self, label: str, types: set[str]) -> None:
        for pair, why in EXCLUSIVE_SIBLINGS:
            if pair <= types:
                self.err(f"{label}: {why} (use separate device groups)")

    def check(self, node: PlanNode, parent_tp: str, dg_protocols: set[str],
              existing_children: set[str] = frozenset()) -> _Checked | None:
        tp = self.schema.child_type_path(parent_tp, node.type)
        if tp is None:
            where = parent_tp.rsplit("/", 1)[-1] if parent_tp else "the root"
            self.err(f"{node.label}: a {node.type} cannot be placed under {where}")
            return None
        info = self.schema.resolve(tp)
        if info is None:
            self.err(f"{node.label}: {node.type} is not in the schema for this IxNetwork build")
            return None
        if not node.auto and not info.raw.get("add"):
            self.err(f"{node.label}: {node.type} objects are created automatically by IxNetwork")
        if node.auto and node.type in REQUIREMENTS and not (REQUIREMENTS[node.type] & dg_protocols):
            need = " or ".join(sorted(REQUIREMENTS[node.type]))
            self.err(f"{node.label}: needs {need} in the same device group (IxNetwork only creates "
                     f"{node.type} then)")
        if not node.auto:
            self.check_name(node.plain.get("name"), node.label)
        meta = self.schema.attribute_meta(tp)
        plain: dict[str, Any] = {}
        mv: dict[str, Any] = {}
        fmts: dict[str, str | None] = {}
        has_nest: dict[str, bool] = {}
        items = list(node.plain.items()) + [(k.lstrip("?"), v) for k, v in node.mv.items()]
        declared_mv = {k for k in node.mv if not k.startswith("?")}
        for attr, raw in items:
            m = meta.get(attr)
            if m is None:
                close = compiler._suggest(attr, sorted(meta))
                self.err(f"{node.label}: {node.type} has no attribute '{attr}'" + (f" (did you mean {close}?)" if close else ""))
                continue
            if m.get("deprecated"):
                self.err(f"{node.label}.{attr}: deprecated on this IxNetwork build")
                continue
            if m["kind"] == "multivalue":
                try:
                    spec = parse_value(raw, f"{node.label}.{attr}")
                except ValueSyntaxError as exc:
                    self.err(str(exc))
                    continue
                mvm = m.get("mv") or {}
                patterns = [p for p in mvm.get("availablePatterns") or [] if p not in ("shared", "subset")]
                for e in validate_value(spec, f"{node.label}.{attr}", mvm.get("format"), mvm.get("enums"),
                                        patterns or None):
                    self.err(e)
                mv[attr] = spec
                fmts[attr] = mvm.get("format")
                default = mvm.get("default")
                has_nest[attr] = bool(default.get("nest")) if isinstance(default, dict) else True
            elif attr in declared_mv:
                self.err(f"{node.label}.{attr}: expected a per-device attribute but it is {m['kind']} here")
            elif m["kind"] == "readonly":
                self.err(f"{node.label}.{attr}: read-only")
            else:
                if node.type == "topology" and attr in ("ports", "vports"):
                    continue  # handled by check_ports
                value, e = compiler.convert_plain(m, attr, raw, self.index)
                if e:
                    self.err(f"{node.label}.{e}")
                else:
                    plain[attr] = value
        # protocols that decide which auto objects exist: a device group's own stack; a network
        # group (and its pools / route properties) inherits its parent device group's
        own = _protocols_in(node) if node.type == "deviceGroup" else dg_protocols
        self.check_exclusive(node.label, {c.type for c in node.children} | existing_children)
        children = [c for c in (self.check(ch, tp, own) for ch in node.children) if c]
        return _Checked(node, tp, plain, mv, fmts, has_nest, children)


# ---------------------------------------------------------------------------- compile

def compile_subtree(ck: _Checked, xpath: str, is_anchor: bool, out: list[dict[str, Any]],
                    planned: list[tuple[_Checked, str]]) -> None:
    planned.append((ck, xpath))
    if not is_anchor and (ck.plain or not ck.node.auto):
        out.append({"xpath": xpath, **ck.plain})
    levels = nest_levels(xpath)
    for attr, spec in ck.mv.items():
        out.extend(compile_value(xpath, attr, spec, levels if ck.has_nest.get(attr, True) else 0))
    counters: dict[str, int] = {}
    for child in ck.children:
        n = child.node
        if n.singleton:
            cx = f"{xpath}/{n.type}"
        elif n.index is not None:
            cx = f"{xpath}/{n.type}[{n.index}]"
        else:
            counters[n.type] = counters.get(n.type, 0) + 1
            cx = f"{xpath}/{n.type}[{counters[n.type]}]"
        compile_subtree(child, cx, False, out, planned)


# ---------------------------------------------------------------------------- engine

def _units(spec: ConfigSpec, index: ConfigIndex) -> tuple[list[Unit], list[str]]:
    """Plan every unit; collect problems instead of stopping at the first one."""
    units: list[Unit] = []
    errors: list[str] = []
    for i, t in enumerate(spec.topologies):
        try:
            units.append(Unit(f"topologies[{i}]", topology_plan(t, f"topologies[{i}]")))
        except PlanError as exc:
            errors += exc.errors
    for j, ext in enumerate(spec.extend):
        try:
            parent = index.resolve(ext.parent)
            for plan in extension_plans(parent.type, ext.add, f"extend[{j}]"):
                units.append(Unit(plan.label, plan, parent=parent))
        except PlanError as exc:
            errors += exc.errors
        except IxToolError as exc:
            errors.append(f"extend[{j}].parent: {exc.message}")
    return units, errors


def parse_spec(raw: Any) -> ConfigSpec:
    try:
        spec = ConfigSpec.model_validate(raw)
    except ValidationError as exc:
        errs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:30]]
        raise IxToolError(VALIDATION, f"the spec is not valid ({len(exc.errors())} problem(s))", errors=errs,
                          hint="get_guide('spec') describes every field.") from None
    if not spec.topologies and not spec.extend:
        raise IxToolError(VALIDATION, "the spec is empty: give 'topologies' and/or 'extend'")
    return spec


def _post(ixn: Any, parent_href: str, ck: _Checked, ports: list[str]) -> str:
    body = dict(ck.plain)
    if ck.node.type == "topology":
        body["ports"] = ports
    url = f"{parent_href}/{ck.node.type}"
    try:
        created = ixn._connection._create(url, body)
    except Exception:
        compiler._cleanup_failed_create(ixn, url, body.get("name"))
        raise
    return compiler.href_of(created)


def _verify(ixn: Any, anchor_href: str, planned: list[tuple[_Checked, str]]) -> list[str]:
    types = sorted({ck.node.type for ck, _ in planned})
    attrs = sorted({"name", *[a for ck, _ in planned for a in ck.mv]})
    r = rest.select(ixn, anchor_href, properties=attrs,
                    children=[rest.child(f"^({'|'.join(types)})$", attrs)], inlines=rest.MV_INLINES)
    found: dict[str, dict[str, Any]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "xpath" in o and not rest.is_multivalue(o):
                found[o["xpath"]] = o
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(r)
    problems = []
    for ck, xp in planned:
        node = found.get(xp)
        if node is None:
            problems.append(f"{ck.node.label}: {ck.node.type} was not created at {xp}")
            continue
        want = ck.node.plain.get("name")
        if want and not ck.node.auto and node.get("name") != want:
            problems.append(f"{ck.node.label}: name is '{node.get('name')}', expected '{want}'")
        for attr, spec in ck.mv.items():
            got = mv_to_value(node.get(attr) or {}) if rest.is_multivalue(node.get(attr)) else node.get(attr)
            if not same_value(spec, got, ck.fmt.get(attr)):
                problems.append(f"{ck.node.label}.{attr}: IxNetwork has {got!r}")
    return problems


def apply(ixn: Any, schema: SchemaIndex, raw_spec: Any, *, dry_run: bool = False,
          rollback_on_error: bool = True) -> dict[str, Any]:
    spec = parse_spec(raw_spec)
    index = ConfigIndex.build(ixn, props=["name", "ports"])
    units, plan_errors = _units(spec, index)
    v = _Validator(schema, index)
    v.errors += plan_errors
    checked: list[tuple[Unit, _Checked]] = []
    topo_specs = {f"topologies[{i}]": t for i, t in enumerate(spec.topologies)}
    for unit in units:
        if unit.parent is None:
            v.check_ports(unit, topo_specs[unit.label].ports)
            ctx: set[str] = set()
            parent_tp = ""
        else:
            parent_tp = unit.parent.type_path
            dg = _nearest_device_group(index, unit.parent)
            ctx = (_existing_protocols(index, dg) if dg else set()) | _protocols_in(unit.plan) | {unit.plan.type}
        if unit.parent is not None:  # a new child must also be compatible with the parent's existing children
            existing = [c.type for c in index.children(unit.parent)]
            v.check_exclusive(unit.label, set(existing) | {unit.plan.type})
            if unit.plan.type in ("ipv4PrefixPools", "ipv6PrefixPools") and unit.plan.type in existing:
                v.err(f"{unit.label}: {unit.parent.name or unit.parent.xpath} already has an "
                      f"{unit.plan.type[:4]} pool (one IPv4 and one IPv6 pool per network group)")
        ck = v.check(unit.plan, parent_tp, ctx)
        if ck:
            checked.append((unit, ck))
    if v.errors:
        raise IxToolError(VALIDATION, f"{len(v.errors)} problem(s) in the spec; nothing was changed",
                          errors=v.errors[:40], hint="describe_node_type lists attributes, formats and enums.")

    if dry_run:
        plans = []
        for unit, ck in checked:
            entries: list[dict[str, Any]] = []
            planned: list[tuple[_Checked, str]] = []
            anchor = f"{unit.parent.xpath if unit.parent else ''}/{ck.node.type}[new]"
            compile_subtree(ck, anchor, True, entries, planned)
            plans.append({"unit": unit.label, "create": ck.node.type, "name": ck.node.plain.get("name"),
                          "under": unit.parent.xpath if unit.parent else "/", "objects": len(planned),
                          "import_entries": entries[:MAX_DRY_RUN_ENTRIES],
                          "import_entries_total": len(entries)})
        return {"dry_run": True, "valid": True, "units": plans}

    created: list[tuple[str, dict[str, Any]]] = []
    root = rest.root(ixn)
    try:
        for unit, ck in checked:
            parent_href = unit.parent.href if unit.parent else root
            href = _post(ixn, parent_href, ck, unit.ports)
            xpath = rest.select(ixn, href, properties=["name"])[0]["xpath"]
            summary = {"unit": unit.label, "type": ck.node.type, "name": ck.node.plain.get("name"), "xpath": xpath}
            created.append((href, summary))
            entries: list[dict[str, Any]] = []
            planned: list[tuple[_Checked, str]] = []
            compile_subtree(ck, xpath, True, entries, planned)
            outcome = compiler.run_import(ixn, entries)
            problems = outcome.errors + _verify(ixn, href, planned)
            if problems:
                raise IxToolError(IXN_ERROR, f"IxNetwork did not build {unit.label} as specified",
                                  errors=problems[:40])
            summary["objects"] = len(planned)
            if outcome.warnings:
                summary["warnings"] = outcome.warnings[:10]
    except Exception as exc:
        rolled = []
        if rollback_on_error:
            for href, summary in reversed(created):
                try:
                    ixn._connection._delete(href)
                    rolled.append(summary["xpath"])
                except Exception as cleanup_exc:  # noqa: BLE001
                    log.warning("rollback of %s failed: %s", href, cleanup_exc)
        err = exc if isinstance(exc, IxToolError) else map_exception(exc)
        extra = dict(err.extra)
        extra.update({"rolled_back": rolled or None,
                      "kept": None if rollback_on_error else [s["xpath"] for _, s in created] or None})
        raise IxToolError(err.code, err.message, err.hint, **extra) from exc
    result: dict[str, Any] = {"created": [s for _, s in created]}
    warn = compiler.on_the_fly_warning(ixn)
    if warn:
        result["warnings"] = [warn]
    return result
