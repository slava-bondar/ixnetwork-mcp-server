"""Curated declarative specs for apply_config and their translation to a plan tree.

The specs mirror the NGPF hierarchy (see guide 'spec'). Every model has friendly keys for
the common knobs, mapped to REST attribute names verified against IxNetwork 10.00 (see
tests/unit/test_specs.py), plus an ``attributes`` passthrough (REST name -> value) for
everything else. Per-device values accept the Value language (values.py).

``to_plan`` turns a spec into ``PlanNode`` trees:
  * created nodes   - new objects (their plain attributes + name go on the node entry)
  * auto nodes      - objects IxNetwork creates by itself (routerData, *Router, vlan[i],
                      route properties, singleton pools/group lists): only attributes are set
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Value = Any  # validated later against the live/captured format of each attribute


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attributes: dict[str, Any] = Field(default_factory=dict, description="REST name -> value passthrough")


# ---------------------------------------------------------------------------- protocols

class BgpPeerSpec(_Spec):
    name: str | None = None
    dut_ip: Value = None
    type: Value = None                      # internal | external
    local_as: Value = None                  # > 65535 -> 4-byte AS
    local_as_4byte: bool | None = None      # force 4-byte encoding for Value patterns
    bgp_id: Value = None
    bgp_id_same_as_router_id: Value = None
    hold_time: Value = None
    keepalive: Value = None
    md5_key: Value = None
    graceful_restart: Value = None
    bfd: Value = None
    ipv4_unicast: Value = None
    ipv6_unicast: Value = None


class OspfSpec(_Spec):
    name: str | None = None
    area_id: Value = None                   # number or dotted quad
    network_type: Value = None              # pointtopoint | broadcast | pointtomultipoint (v2)
    hello_interval: Value = None
    dead_interval: Value = None
    metric: Value = None
    priority: Value = None
    auth: Literal["none", "password", "md5"] | None = None
    auth_key: Value = None
    auth_key_id: Value = None               # md5 only (OSPFv2)
    instance_id: Value = None               # OSPFv3 only


class IsisSpec(_Spec):
    name: str | None = None
    network_type: Value = None              # pointpoint | broadcast
    level: Value = None                     # level1 | level2 | l1l2
    metric: Value = None
    hello_interval: Value = None            # applied to level 1 and level 2
    dead_interval: Value = None


class IsisRouterSpec(_Spec):
    area_addresses: Value = None
    wide_metric: Value = None
    hostname: Value = None
    te_router_id: Value = None


class RouterSpec(_Spec):
    """DG-level ospfv2Router / ospfv3Router: passthrough attributes only."""


class McastGroupSpec(_Spec):
    start: Value
    count: Value = None
    step: Value = None
    source_mode: Value = None               # include | exclude


class IgmpMldHostSpec(_Spec):
    name: str | None = None
    version: Value = None                   # 1|2|3 or versionN
    groups: McastGroupSpec | None = None


class IgmpMldQuerierSpec(_Spec):
    name: str | None = None
    version: Value = None
    query_interval: Value = None
    robustness: Value = None


class DhcpClientSpec(_Spec):
    name: str | None = None
    broadcast: Value = None                 # v4
    ia_type: Value = None                   # v6: iana | iata | iapd | iana_iapd
    duid_type: Value = None                 # v6


class DhcpPoolSpec(_Spec):
    start: Value
    size: Value = None
    prefix: Value = None
    gateway: Value = None                   # v4
    dns1: Value = None                      # v4
    dns2: Value = None                      # v4
    lease_time: Value = None
    ia_type: Value = None                   # v6


class DhcpServerSpec(_Spec):
    name: str | None = None
    pool: DhcpPoolSpec


# ---------------------------------------------------------------------------- stacks

class Ipv4Spec(_Spec):
    name: str | None = None
    address: Value = None
    prefix: Value = None
    gateway: Value = None
    resolve_gateway: Value = None
    manual_gateway_mac: Value = None
    bgp_peers: list[BgpPeerSpec] = Field(default_factory=list)
    ospf: OspfSpec | None = None
    igmp_host: IgmpMldHostSpec | None = None
    igmp_querier: IgmpMldQuerierSpec | None = None
    dhcp_server: DhcpServerSpec | None = None


class Ipv6Spec(_Spec):
    name: str | None = None
    address: Value = None
    prefix: Value = None
    gateway: Value = None
    resolve_gateway: Value = None
    bgp_peers: list[BgpPeerSpec] = Field(default_factory=list)
    ospf: OspfSpec | None = None
    mld_host: IgmpMldHostSpec | None = None
    mld_querier: IgmpMldQuerierSpec | None = None
    dhcp_server: DhcpServerSpec | None = None


class VlanSpec(_Spec):
    id: Value = None
    priority: Value = None
    tpid: Value = None


class EthernetSpec(_Spec):
    name: str | None = None
    mac: Value = None
    mtu: Value = None
    vlans: list[VlanSpec] = Field(default_factory=list)
    ipv4: Ipv4Spec | None = None
    ipv6: Ipv6Spec | None = None
    isis: IsisSpec | None = None
    dhcpv4_client: DhcpClientSpec | None = None
    dhcpv6_client: DhcpClientSpec | None = None


class LoopbackSpec(_Spec):
    name: str | None = None
    address: Value = None
    prefix: Value = None
    bgp_peers: list[BgpPeerSpec] = Field(default_factory=list)
    ospf: OspfSpec | None = None


class RoutePropertySpec(_Spec):
    next_hop: Value = None                  # an address; omitted = same as local IP
    origin: Value = None                    # BGP: igp | egp | incomplete; OSPF/IS-IS: routeOrigin enums
    local_pref: Value = None                # BGP
    med: Value = None                       # BGP
    metric: Value = None                    # OSPF / IS-IS


class PoolSpec(_Spec):
    name: str | None = None
    network: Value = None                   # first network address (or a Value pattern)
    prefix_length: Value = None
    count: Value = None                     # prefixes per pool (numberOfAddressesAsy)
    address_step: Value = None              # step between those prefixes (prefixAddrStep)
    bgp: RoutePropertySpec | None = None    # needs an IPv4 BGP peer in the parent device group
    bgp_v6: RoutePropertySpec | None = None  # needs an IPv6 BGP peer
    ospf: RoutePropertySpec | None = None   # OSPFv2 (v4 pools) / OSPFv3 (v6 pools)
    isis: RoutePropertySpec | None = None


class NetworkGroupSpec(_Spec):
    """IxNetwork allows at most one IPv4 and one IPv6 pool per network group."""
    name: str | None = None
    multiplier: int = Field(1, ge=1)
    ipv4_pool: PoolSpec | None = None
    ipv6_pool: PoolSpec | None = None
    device_groups: list["DeviceGroupSpec"] = Field(default_factory=list)


class DeviceGroupSpec(_Spec):
    name: str
    multiplier: int = Field(ge=1, description="number of emulated devices (IxNetwork's default is 10)")
    enabled: Value = None
    router_id: Value = None                 # needs BGP or OSPF in this device group
    ethernet: EthernetSpec | None = None
    loopback_v4: LoopbackSpec | None = None
    loopback_v6: LoopbackSpec | None = None
    ospfv2_router: RouterSpec | None = None
    ospfv3_router: RouterSpec | None = None
    isis_router: IsisRouterSpec | None = None
    network_groups: list[NetworkGroupSpec] = Field(default_factory=list)
    device_groups: list["DeviceGroupSpec"] = Field(default_factory=list)


class TopologySpec(_Spec):
    name: str
    ports: list[str] = Field(min_length=1, description="vport names; each port can belong to one topology")
    device_groups: list[DeviceGroupSpec] = Field(default_factory=list)


class ExtendSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent: str
    add: dict[str, Any]


class ConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topologies: list[TopologySpec] = Field(default_factory=list)
    extend: list[ExtendSpec] = Field(default_factory=list)


NetworkGroupSpec.model_rebuild()
DeviceGroupSpec.model_rebuild()

# which spec fields may be added under an existing object of a given type (extend mode)
EXTEND_FIELDS: dict[str, dict[str, Any]] = {
    "topology": {"device_groups": DeviceGroupSpec},
    "deviceGroup": {"ethernet": EthernetSpec, "loopback_v4": LoopbackSpec, "loopback_v6": LoopbackSpec,
                    "network_groups": NetworkGroupSpec, "device_groups": DeviceGroupSpec},
    "networkGroup": {"ipv4_pool": PoolSpec, "ipv6_pool": PoolSpec, "device_groups": DeviceGroupSpec},
    "ethernet": {"ipv4": Ipv4Spec, "ipv6": Ipv6Spec, "isis": IsisSpec, "dhcpv4_client": DhcpClientSpec,
                 "dhcpv6_client": DhcpClientSpec},
    "ipv4": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec, "igmp_host": IgmpMldHostSpec,
             "igmp_querier": IgmpMldQuerierSpec, "dhcp_server": DhcpServerSpec},
    "ipv6": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec, "mld_host": IgmpMldHostSpec,
             "mld_querier": IgmpMldQuerierSpec, "dhcp_server": DhcpServerSpec},
    "ipv4Loopback": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec},
    "ipv6Loopback": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec},
}


# ---------------------------------------------------------------------------- plan tree

@dataclass
class PlanNode:
    type: str                                   # REST child name
    label: str                                  # spec path for messages
    plain: dict[str, Any] = field(default_factory=dict)
    mv: dict[str, Any] = field(default_factory=dict)
    children: list["PlanNode"] = field(default_factory=list)
    auto: bool = False                          # created by IxNetwork: only set attributes
    singleton: bool = False                     # xpath segment without [i]
    index: int | None = None                    # fixed position for auto collections (vlan[i])
    requires: str | None = None                 # why an auto node should exist (for messages)

    @property
    def name(self) -> str | None:
        return self.plain.get("name")


class PlanError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _set(mv: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        mv[key] = value


def _passthrough(node: PlanNode, attrs: dict[str, Any]) -> None:
    """Keep `attributes` raw; the builder decides plain vs multivalue from the schema."""
    node.mv.update({f"?{k}": v for k, v in attrs.items()})  # '?' = kind decided by the builder


def _bgp(spec: BgpPeerSpec, v6: bool, label: str) -> PlanNode:
    n = PlanNode("bgpIpv6Peer" if v6 else "bgpIpv4Peer", label)
    if spec.name:
        n.plain["name"] = spec.name
    m = n.mv
    _set(m, "dutIp", spec.dut_ip)
    _set(m, "type", spec.type)
    if spec.local_as is not None:
        four = spec.local_as_4byte
        if four is None:
            four = isinstance(spec.local_as, (int, str)) and str(spec.local_as).isdigit() and int(spec.local_as) > 65535
        if four:
            m["enable4ByteAs"] = True
            m["localAs4Bytes"] = spec.local_as
        else:
            m["localAs2Bytes"] = spec.local_as
    _set(m, "bgpId", spec.bgp_id)
    _set(m, "enableBgpIdSameAsRouterId" if v6 else "enableBgpIdSameasRouterId", spec.bgp_id_same_as_router_id)
    _set(m, "holdTimer", spec.hold_time)
    _set(m, "keepaliveTimer", spec.keepalive)
    if spec.md5_key is not None:
        m["authentication"] = "md5"
        m["md5Key"] = spec.md5_key
    _set(m, "enableGracefulRestart", spec.graceful_restart)
    if spec.bfd is not None:
        if v6:
            raise PlanError([f"{label}.bfd: not available on IPv6 BGP peers in this build"])
        m["enableBfdRegistration"] = spec.bfd
    _set(m, "capabilityIpV4Unicast", spec.ipv4_unicast)
    _set(m, "capabilityIpV6Unicast", spec.ipv6_unicast)
    _passthrough(n, spec.attributes)
    return n


def _ospf(spec: OspfSpec, v3: bool, label: str) -> PlanNode:
    n = PlanNode("ospfv3" if v3 else "ospfv2", label)
    if spec.name:
        n.plain["name"] = spec.name
    m = n.mv
    if spec.area_id is not None:
        if isinstance(spec.area_id, str) and spec.area_id.count(".") == 3:
            if not v3:
                m["typeAreaId"] = "ip"
            m["areaIdIp"] = spec.area_id
        else:
            m["areaId"] = spec.area_id
    _set(m, "networkType", spec.network_type)
    _set(m, "helloInterval", spec.hello_interval)
    _set(m, "deadInterval", spec.dead_interval)
    _set(m, "linkMetric" if v3 else "metric", spec.metric)
    _set(m, "priority", spec.priority)
    if spec.auth is not None:
        if v3:
            raise PlanError([f"{label}.auth: set OSPFv3 authentication through attributes"])
        m["authentication"] = "null" if spec.auth == "none" else spec.auth
        if spec.auth == "password":
            _set(m, "authenticationPassword", spec.auth_key)
        elif spec.auth == "md5":
            _set(m, "md5Key", spec.auth_key)
            _set(m, "md5KeyId", spec.auth_key_id)
    if spec.instance_id is not None:
        if not v3:
            raise PlanError([f"{label}.instance_id: OSPFv3 only"])
        m["instanceId"] = spec.instance_id
    _passthrough(n, spec.attributes)
    return n


def _mcast_host(spec: IgmpMldHostSpec, mld: bool, label: str) -> PlanNode:
    n = PlanNode("mldHost" if mld else "igmpHost", label)
    if spec.name:
        n.plain["name"] = spec.name
    if spec.version is not None:
        v = str(spec.version)
        n.mv["versionType"] = v if v.startswith("version") else f"version{v}"
    if spec.groups is not None:
        n.plain["noOfGrpRanges"] = 1
        g = PlanNode("mldMcastIPv6GroupList" if mld else "igmpMcastIPv4GroupList", f"{label}.groups",
                     auto=True, singleton=True)
        _set(g.mv, "startMcastAddr", spec.groups.start)
        _set(g.mv, "mcastAddrCnt", spec.groups.count)
        _set(g.mv, "mcastAddrIncr", spec.groups.step)
        _set(g.mv, "sourceMode", spec.groups.source_mode)
        _passthrough(g, spec.groups.attributes)
        n.children.append(g)
    _passthrough(n, spec.attributes)
    return n


def _querier(spec: IgmpMldQuerierSpec, mld: bool, label: str) -> PlanNode:
    n = PlanNode("mldQuerier" if mld else "igmpQuerier", label)
    if spec.name:
        n.plain["name"] = spec.name
    if spec.version is not None:
        v = str(spec.version)
        n.mv["versionType"] = v if v.startswith("version") else f"version{v}"
    _set(n.mv, "generalQueryInterval", spec.query_interval)
    _set(n.mv, "robustnessVariable", spec.robustness)
    _passthrough(n, spec.attributes)
    return n


def _dhcp_server(spec: DhcpServerSpec, v6: bool, label: str) -> PlanNode:
    n = PlanNode("dhcpv6server" if v6 else "dhcpv4server", label)
    if spec.name:
        n.plain["name"] = spec.name
    n.plain["poolCount"] = 1
    p = PlanNode("dhcp6ServerSessions" if v6 else "dhcp4ServerSessions", f"{label}.pool", auto=True, singleton=True)
    pool = spec.pool
    _set(p.mv, "ipAddress", pool.start)
    _set(p.mv, "poolSize", pool.size)
    _set(p.mv, "ipPrefix", pool.prefix)
    _set(p.mv, "defaultLeaseTime", pool.lease_time)
    if v6:
        _set(p.mv, "iaType", pool.ia_type)
        if pool.gateway is not None or pool.dns1 is not None or pool.dns2 is not None:
            raise PlanError([f"{label}.pool: gateway/dns are DHCPv4 pool settings (use the server's attributes for v6 DNS)"])
    else:
        _set(p.mv, "ipGateway", pool.gateway)
        _set(p.mv, "ipDns1", pool.dns1)
        _set(p.mv, "ipDns2", pool.dns2)
        if pool.ia_type is not None:
            raise PlanError([f"{label}.pool.ia_type: DHCPv6 only"])
    _passthrough(p, pool.attributes)
    n.children.append(p)
    _passthrough(n, spec.attributes)
    return n


def _dhcp_client(spec: DhcpClientSpec, v6: bool, label: str) -> PlanNode:
    n = PlanNode("dhcpv6client" if v6 else "dhcpv4client", label)
    if spec.name:
        n.plain["name"] = spec.name
    if v6:
        _set(n.mv, "dhcp6IaType", spec.ia_type)
        _set(n.mv, "dhcp6DuidType", spec.duid_type)
        if spec.broadcast is not None:
            raise PlanError([f"{label}.broadcast: DHCPv4 only"])
    else:
        _set(n.mv, "dhcp4Broadcast", spec.broadcast)
        if spec.ia_type is not None or spec.duid_type is not None:
            raise PlanError([f"{label}: ia_type/duid_type are DHCPv6 settings"])
    _passthrough(n, spec.attributes)
    return n


def _ip(spec: Union[Ipv4Spec, Ipv6Spec], v6: bool, label: str) -> PlanNode:
    n = PlanNode("ipv6" if v6 else "ipv4", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "address", spec.address)
    _set(n.mv, "prefix", spec.prefix)
    _set(n.mv, "gatewayIp", spec.gateway)
    _set(n.mv, "resolveGateway", spec.resolve_gateway)
    if not v6:
        _set(n.mv, "manualGatewayMac", spec.manual_gateway_mac)
    for i, b in enumerate(spec.bgp_peers):
        n.children.append(_bgp(b, v6, f"{label}.bgp_peers[{i}]"))
    if spec.ospf:
        n.children.append(_ospf(spec.ospf, v6, f"{label}.ospf"))
    host = spec.mld_host if v6 else spec.igmp_host  # type: ignore[union-attr]
    if host:
        n.children.append(_mcast_host(host, v6, f"{label}.{'mld' if v6 else 'igmp'}_host"))
    querier = spec.mld_querier if v6 else spec.igmp_querier  # type: ignore[union-attr]
    if querier:
        n.children.append(_querier(querier, v6, f"{label}.{'mld' if v6 else 'igmp'}_querier"))
    if spec.dhcp_server:
        n.children.append(_dhcp_server(spec.dhcp_server, v6, f"{label}.dhcp_server"))
    _passthrough(n, spec.attributes)
    return n


def _isis(spec: IsisSpec, label: str) -> PlanNode:
    n = PlanNode("isisL3", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "networkType", spec.network_type)
    _set(n.mv, "levelType", spec.level)
    _set(n.mv, "interfaceMetric", spec.metric)
    if spec.hello_interval is not None:
        n.mv["level1HelloInterval"] = n.mv["level2HelloInterval"] = spec.hello_interval
    if spec.dead_interval is not None:
        n.mv["level1DeadInterval"] = n.mv["level2DeadInterval"] = spec.dead_interval
    _passthrough(n, spec.attributes)
    return n


def _ethernet(spec: EthernetSpec, label: str) -> PlanNode:
    n = PlanNode("ethernet", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "mac", spec.mac)
    _set(n.mv, "mtu", spec.mtu)
    if spec.vlans:
        n.plain["vlanCount"] = len(spec.vlans)
        n.mv["enableVlans"] = True
        for i, v in enumerate(spec.vlans, 1):
            vn = PlanNode("vlan", f"{label}.vlans[{i - 1}]", auto=True, index=i)
            _set(vn.mv, "vlanId", v.id)
            _set(vn.mv, "priority", v.priority)
            _set(vn.mv, "tpid", v.tpid)
            _passthrough(vn, v.attributes)
            n.children.append(vn)
    if spec.ipv4:
        n.children.append(_ip(spec.ipv4, False, f"{label}.ipv4"))
    if spec.ipv6:
        n.children.append(_ip(spec.ipv6, True, f"{label}.ipv6"))
    if spec.isis:
        n.children.append(_isis(spec.isis, f"{label}.isis"))
    if spec.dhcpv4_client:
        n.children.append(_dhcp_client(spec.dhcpv4_client, False, f"{label}.dhcpv4_client"))
    if spec.dhcpv6_client:
        n.children.append(_dhcp_client(spec.dhcpv6_client, True, f"{label}.dhcpv6_client"))
    _passthrough(n, spec.attributes)
    return n


def _loopback(spec: LoopbackSpec, v6: bool, label: str) -> PlanNode:
    n = PlanNode("ipv6Loopback" if v6 else "ipv4Loopback", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "address", spec.address)
    _set(n.mv, "prefix", spec.prefix)
    for i, b in enumerate(spec.bgp_peers):
        n.children.append(_bgp(b, v6, f"{label}.bgp_peers[{i}]"))
    if spec.ospf:
        n.children.append(_ospf(spec.ospf, v6, f"{label}.ospf"))
    _passthrough(n, spec.attributes)
    return n


def _route_prop(kind: str, spec: RoutePropertySpec, v6_pool: bool, label: str) -> PlanNode:
    types = {"bgp": "bgpIPRouteProperty", "bgp_v6": "bgpV6IPRouteProperty",
             "ospf": "ospfv3RouteProperty" if v6_pool else "ospfRouteProperty", "isis": "isisL3RouteProperty"}
    n = PlanNode(types[kind], label, auto=True, index=1, requires=kind)
    m = n.mv
    if kind.startswith("bgp"):
        if spec.next_hop is not None:
            m["nextHopType"] = "manual"
            ip6 = isinstance(spec.next_hop, str) and ":" in spec.next_hop
            m["nextHopIPType"] = "ipv6" if ip6 else "ipv4"
            m["ipv6NextHop" if ip6 else "ipv4NextHop"] = spec.next_hop
        _set(m, "origin", spec.origin)
        if spec.local_pref is not None:
            m["enableLocalPreference"] = True
            m["localPreference"] = spec.local_pref
        if spec.med is not None:
            m["enableMultiExitDiscriminator"] = True
            m["multiExitDiscriminator"] = spec.med
        if spec.metric is not None:
            raise PlanError([f"{label}.metric: use med for BGP"])
    else:
        _set(m, "metric", spec.metric)
        _set(m, "routeOrigin", spec.origin)
        bad = [k for k in ("next_hop", "local_pref", "med") if getattr(spec, k) is not None]
        if bad:
            raise PlanError([f"{label}: {bad} are BGP settings"])
    _passthrough(n, spec.attributes)
    return n


def _pool(spec: PoolSpec, v6: bool, label: str) -> PlanNode:
    n = PlanNode("ipv6PrefixPools" if v6 else "ipv4PrefixPools", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "networkAddress", spec.network)
    _set(n.mv, "prefixLength", spec.prefix_length)
    _set(n.mv, "numberOfAddressesAsy", spec.count)
    _set(n.mv, "prefixAddrStep", spec.address_step)
    for kind in ("bgp", "bgp_v6", "ospf", "isis"):
        rp = getattr(spec, kind)
        if rp is not None:
            n.children.append(_route_prop(kind, rp, v6, f"{label}.{kind}"))
    _passthrough(n, spec.attributes)
    return n


def _network_group(spec: NetworkGroupSpec, label: str) -> PlanNode:
    n = PlanNode("networkGroup", label, plain={"multiplier": spec.multiplier})
    if spec.name:
        n.plain["name"] = spec.name
    if spec.ipv4_pool:
        n.children.append(_pool(spec.ipv4_pool, False, f"{label}.ipv4_pool"))
    if spec.ipv6_pool:
        n.children.append(_pool(spec.ipv6_pool, True, f"{label}.ipv6_pool"))
    for i, d in enumerate(spec.device_groups):
        n.children.append(_device_group(d, f"{label}.device_groups[{i}]"))
    _passthrough(n, spec.attributes)
    return n


def _device_group(spec: DeviceGroupSpec, label: str) -> PlanNode:
    n = PlanNode("deviceGroup", label, plain={"name": spec.name, "multiplier": spec.multiplier})
    _set(n.mv, "enabled", spec.enabled)
    if spec.ethernet:
        n.children.append(_ethernet(spec.ethernet, f"{label}.ethernet"))
    if spec.loopback_v4:
        n.children.append(_loopback(spec.loopback_v4, False, f"{label}.loopback_v4"))
    if spec.loopback_v6:
        n.children.append(_loopback(spec.loopback_v6, True, f"{label}.loopback_v6"))
    if spec.router_id is not None:
        rd = PlanNode("routerData", f"{label}.router_id", auto=True, index=1, requires="routerData")
        rd.mv["routerId"] = spec.router_id
        n.children.append(rd)
    for key, rtype in (("ospfv2_router", "ospfv2Router"), ("ospfv3_router", "ospfv3Router"), ("isis_router", "isisL3Router")):
        rs = getattr(spec, key)
        if rs is None:
            continue
        r = PlanNode(rtype, f"{label}.{key}", auto=True, index=1, requires=rtype)
        if isinstance(rs, IsisRouterSpec):
            _set(r.mv, "areaAddresses", rs.area_addresses)
            _set(r.mv, "enableWideMetric", rs.wide_metric)
            if rs.hostname is not None:
                r.mv["enableHostName"] = True
                r.mv["hostName"] = rs.hostname
            if rs.te_router_id is not None:
                r.mv["enableTE"] = True
                r.mv["tERouterId"] = rs.te_router_id
        _passthrough(r, rs.attributes)
        n.children.append(r)
    for i, g in enumerate(spec.network_groups):
        n.children.append(_network_group(g, f"{label}.network_groups[{i}]"))
    for i, d in enumerate(spec.device_groups):
        n.children.append(_device_group(d, f"{label}.device_groups[{i}]"))
    _passthrough(n, spec.attributes)
    return n


def topology_plan(spec: TopologySpec, label: str) -> PlanNode:
    n = PlanNode("topology", label, plain={"name": spec.name})
    for i, d in enumerate(spec.device_groups):
        n.children.append(_device_group(d, f"{label}.device_groups[{i}]"))
    _passthrough(n, spec.attributes)
    return n


_BUILDERS = {
    DeviceGroupSpec: lambda s, lbl: _device_group(s, lbl),
    NetworkGroupSpec: lambda s, lbl: _network_group(s, lbl),
    EthernetSpec: lambda s, lbl: _ethernet(s, lbl),
    IsisSpec: lambda s, lbl: _isis(s, lbl),
}


def extension_plans(parent_type: str, add: dict[str, Any], label: str) -> list[PlanNode]:
    """Plan nodes to create under an existing object of ``parent_type``."""
    allowed = EXTEND_FIELDS.get(parent_type)
    if allowed is None:
        raise PlanError([f"{label}: nothing can be added to a {parent_type} with apply_config; use add_node"])
    unknown = set(add) - set(allowed)
    if unknown:
        raise PlanError([f"{label}.add: {sorted(unknown)} cannot be added to a {parent_type} "
                         f"(allowed: {sorted(allowed)})"])
    v6_parent = parent_type in ("ipv6", "ipv6Loopback")
    plans: list[PlanNode] = []
    for key, raw in add.items():
        model = allowed[key]
        items = raw if isinstance(raw, list) else [raw]
        for i, item in enumerate(items):
            lbl = f"{label}.add.{key}" + (f"[{i}]" if isinstance(raw, list) else "")
            try:
                spec = model.model_validate(item)
            except ValidationError as exc:
                raise PlanError([f"{lbl}.{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                                 for e in exc.errors()[:20]]) from None
            if model is BgpPeerSpec:
                plans.append(_bgp(spec, v6_parent, lbl))
            elif model is OspfSpec:
                plans.append(_ospf(spec, v6_parent, lbl))
            elif model is IgmpMldHostSpec:
                plans.append(_mcast_host(spec, key == "mld_host", lbl))
            elif model is IgmpMldQuerierSpec:
                plans.append(_querier(spec, key == "mld_querier", lbl))
            elif model is DhcpServerSpec:
                plans.append(_dhcp_server(spec, v6_parent, lbl))
            elif model is DhcpClientSpec:
                plans.append(_dhcp_client(spec, key == "dhcpv6_client", lbl))
            elif model is Ipv4Spec or model is Ipv6Spec:
                plans.append(_ip(spec, model is Ipv6Spec, lbl))
            elif model is LoopbackSpec:
                plans.append(_loopback(spec, key == "loopback_v6", lbl))
            elif model is PoolSpec:
                plans.append(_pool(spec, key == "ipv6_pool", lbl))
            else:
                plans.append(_BUILDERS[model](spec, lbl))
    return plans
