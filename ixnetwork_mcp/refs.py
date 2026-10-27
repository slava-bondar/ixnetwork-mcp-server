"""Resolve agent-supplied references to exact configuration objects.

A *ref* may be:
  * a name                 'BGP Peer 1'   (NGPF names are unique within the configuration)
  * a type-qualified name  'vport:Ethernet - 001', 'trafficItem:v4 flows'
  * an xpath               '/topology[1]/deviceGroup[2]'  (positional; shifts after deletions)
  * an href                '/api/v1/sessions/1/ixnetwork/topology/1/deviceGroup/2'

``ConfigIndex.build`` snapshots names/xpaths/hrefs of vports, traffic items and every
topology subtree using one select per topology (xpath=true gives both addresses; ids and
positions differ, so neither is derived from the other).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import rest
from .errors import AMBIGUOUS_REF, NOT_FOUND, IxToolError

# not configuration: learned state, per-port views, tags
_INDEX_SKIP = r"learnedInfo|port|tag"
INDEX_REGEX = rf"^(?!({_INDEX_SKIP})$)[A-Za-z0-9]+$"
# auto-created helper objects hidden from listings unless asked for
AUTO_TYPES = re.compile(
    r"^(connector|tlvProfile|vpnParameter|pbbEVpnParameter|routerData|bridgeData|iptv|Rfc8277LabelStack|"
    r".*Params|dhcp6Iana[0-9]*|dhcp6Iapd[0-9]*|.*List|.*Info|.*Profile|.*TrafficEngineering|bgpSRGB.*)$")

DEFAULT_PROPS = ["name", "count", "multiplier", "status", "stateCounts", "ports"]


@dataclass
class Node:
    href: str
    xpath: str
    type: str
    name: str | None
    props: dict[str, Any] = field(default_factory=dict)
    parent: str | None = None  # parent xpath
    auto: bool = False  # auto-created helper (or inside one, e.g. tlvProfile/defaultTlv/object/...)

    @property
    def type_path(self) -> str:
        return rest.type_path(self.xpath)

    def brief(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "xpath": self.xpath}
        for k in ("multiplier", "count", "status"):
            if isinstance(self.props.get(k), (int, str)) and self.props.get(k) != "":
                out[k] = self.props[k]
        sc = rest.state_counts(self.props.get("stateCounts"))
        if sc and sc["total"]:
            out["sessions"] = sc
        return out


class ConfigIndex:
    def __init__(self, nodes: list[Node]):
        self.nodes = nodes
        self.by_href = {n.href.rstrip("/"): n for n in nodes}
        self.by_xpath = {n.xpath: n for n in nodes}
        self.by_name: dict[str, list[Node]] = {}
        for n in nodes:
            if n.name:
                self.by_name.setdefault(n.name, []).append(n)

    # ------------------------------------------------------------------ build

    @classmethod
    def build(cls, ixn: Any, *, props: list[str] | None = None, topologies: bool = True,
              ports: bool = True, traffic: bool = True) -> "ConfigIndex":
        props = props or DEFAULT_PROPS
        nodes: list[Node] = []
        root = rest.root(ixn)
        if ports or traffic:
            regex = "|".join(x for x, on in (("vport", ports), ("traffic|trafficItem", traffic)) if on)
            r = rest.select(ixn, root, properties=[], children=[rest.child(f"^({regex})$", props)])
            _flatten(r[0], None, nodes, skip_root=True)
        if topologies:
            for t in rest.get(ixn, "topology?includes=name"):
                r = rest.select(ixn, f"{root}/topology/{t['id']}", properties=props,
                                children=[rest.child(INDEX_REGEX, props)])
                _flatten(r[0], None, nodes)
        return cls([n for n in nodes if n.type != "traffic"])

    # ------------------------------------------------------------------ navigation

    def children(self, node: Node) -> list[Node]:
        return [n for n in self.nodes if n.parent == node.xpath]

    def subtree(self, node: Node) -> list[Node]:
        prefix = node.xpath + "/"
        return [n for n in self.nodes if n.xpath.startswith(prefix)]

    # ------------------------------------------------------------------ resolution

    def resolve(self, ref: str, types: Iterable[str] | None = None) -> Node:
        ref = ref.strip()
        wanted = {t.lower() for t in types} if types else None
        if not ref:
            raise IxToolError(NOT_FOUND, "empty ref")
        if ref.startswith("/api/"):
            node = self.by_href.get(ref.rstrip("/"))
            if node:
                return self._check_type(node, wanted, ref)
            raise IxToolError(NOT_FOUND, f"no configuration object with href {ref}",
                              hint="Hrefs change after the configuration is reloaded; use names or re-read.")
        if ref.startswith("/"):
            node = self.by_xpath.get(ref)
            if node:
                return self._check_type(node, wanted, ref)
            raise IxToolError(NOT_FOUND, f"no object at xpath {ref}",
                              hint="Xpath positions shift when objects are deleted; re-read the overview or use names.",
                              candidates=self._near_xpaths(ref))
        typed = re.match(r"^([A-Za-z0-9]+):(.+)$", ref)
        if typed and any(n.type.lower() == typed.group(1).lower() for n in self.nodes):
            wanted = {typed.group(1).lower()}
            ref = typed.group(2).strip()
        matches = self._filter(self.by_name.get(ref, []), wanted)
        if not matches:
            matches = self._filter([n for n in self.nodes if (n.name or "").lower() == ref.lower()], wanted)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise IxToolError(AMBIGUOUS_REF, f"'{ref}' matches {len(matches)} objects",
                              hint="Qualify the ref with its type ('type:name') or use one of the xpaths.",
                              candidates=[m.brief() for m in matches[:10]])
        similar = [n for n in self._filter(self.nodes, wanted) if ref.lower() in (n.name or "").lower()]
        raise IxToolError(NOT_FOUND, f"no object named '{ref}'" + (f" of type {sorted(wanted)}" if wanted else ""),
                          candidates=[{"name": n.name, "type": n.type, "xpath": n.xpath} for n in similar[:10]] or None,
                          hint=None if similar else "Use get_session_overview or find_nodes to see what exists.")

    @staticmethod
    def _filter(nodes: list[Node], wanted: set[str] | None) -> list[Node]:
        return [n for n in nodes if wanted is None or n.type.lower() in wanted]

    @staticmethod
    def _check_type(node: Node, wanted: set[str] | None, ref: str) -> Node:
        if wanted and node.type.lower() not in wanted:
            raise IxToolError(NOT_FOUND, f"{ref} is a {node.type}, expected one of {sorted(wanted)}")
        return node

    def _near_xpaths(self, xpath: str) -> list[str] | None:
        tp = rest.type_path(xpath)
        near = [n.xpath for n in self.nodes if n.type_path == tp]
        return near[:10] or None


def _flatten(obj: dict[str, Any], parent: str | None, out: list[Node], skip_root: bool = False,
             parent_auto: bool = False) -> None:
    xpath = obj.get("xpath", "")
    me, auto = parent, parent_auto
    if not skip_root and xpath and not rest.is_multivalue(obj):
        ntype = rest.node_type(xpath)
        auto = parent_auto or bool(AUTO_TYPES.match(ntype))
        node = Node(href=obj["href"].rstrip("/"), xpath=xpath, type=ntype,
                    name=obj.get("name"), parent=parent, auto=auto,
                    props={k: v for k, v in obj.items() if not isinstance(v, (list, dict)) or k in ("stateCounts", "ports")})
        out.append(node)
        me = xpath
    for kid in rest.child_nodes(obj):
        _flatten(kid, me, out, parent_auto=auto)


def parse_device_ranges(text: str, count: int | None = None) -> list[int]:
    """'1-3,7' -> [1, 2, 3, 7] (1-based device indices within one protocol object). `count` is the
    object's device count; indices beyond it are an error."""
    from .errors import VALIDATION

    out: set[int] = set()
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", part)
        if not m:
            raise IxToolError(VALIDATION, f"devices: '{part}' is not an index or a range like '1-3'")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo:
            raise IxToolError(VALIDATION, f"devices: '{part}' is not a valid 1-based range")
        out.update(range(lo, hi + 1))
    if not out:
        raise IxToolError(VALIDATION, "devices: no indices given")
    if count is not None and max(out) > count:
        raise IxToolError(VALIDATION, f"devices: index {max(out)} is beyond the object's {count} device(s)")
    return sorted(out)
