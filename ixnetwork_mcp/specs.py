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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Value = Any  # validated later against the live/captured format of each attribute


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attributes: dict[str, Any] = Field(default_factory=dict, description="REST name -> value passthrough")


# ---------------------------------------------------------------------------- protocols

def _admin_and_number(text: str, what: str) -> tuple[str, int]:
    admin, sep, num = str(text).rpartition(":")
    if not sep or not admin or not num.isdigit():
        raise ValueError(f"{what}: expected 'AS:number' or 'IP:number', got '{text}'")
    return admin, int(num)


class RouteTargetSpec(_Spec):
    """A route target; the shorthand "65001:100" / "4200000000:100" / "192.0.2.1:100" fills the fields."""
    type: Value = None                      # as | ip | as4 (targetType)
    as_number: Value = None                 # targetAsNumber, or targetAs4Number with type as4
    ip: Value = None                        # targetIpAddress
    number: Value = None                    # targetAssignedNumber

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        admin, num = _admin_and_number(v, "route target")
        if admin.isdigit():
            return {"type": "as4" if int(admin) > 65535 else "as", "as_number": int(admin), "number": num}
        return {"type": "ip", "ip": admin, "number": num}


class _EviSpec(_Spec):
    name: str | None = None
    count: int | None = Field(None, ge=1)   # EVIs per peer (the EVI's multiplier); one EVI object per peer
    rd: str | None = None                   # shorthand "65001:100" / "192.0.2.1:100" for the rd_* fields
    rd_type: Value = None                   # as | ip | as4 (rdType)
    rd_as: Value = None                     # rdASNumber
    rd_ip: Value = None                     # rdIpAddress (with autoConfigureRdIpAddress=false)
    rd_evi: Value = None                    # rdEvi: the number after the colon
    route_targets: list[RouteTargetSpec] = Field(default_factory=list)     # export RTs; import = the same
    l3_route_targets: list[RouteTargetSpec] = Field(default_factory=list)  # L3 VNI RTs (Type-5 routes)
    ethernet_tag_id: Value = None           # broadcastDomainV4.ethernetTagId


class EvpnVxlanSpec(_EviSpec):
    """An EVPN instance (EVI) over VXLAN on an IPv4 BGP peer (bgpIPv4EvpnVXLAN)."""


class EvpnMplsSpec(_EviSpec):
    """An EVPN instance (EVI) over MPLS on an IPv4 BGP peer (bgpIPv4EvpnEvi). IxNetwork needs an
    Ethernet segment on the peer for it (also for a VXLAN EVI with count > 1): the peer gets one
    (ethernetSegmentsCountV4=1)."""
    esi: Value = None                       # the segment's ESI (bgpEthernetSegmentV4.esiValue; default 0)


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
    evpn_vxlan: EvpnVxlanSpec | None = None  # IPv4 peers; one EVI (VXLAN or MPLS) per peer; sets the EVPN capability
    evpn_mpls: EvpnMplsSpec | None = None


class LdpSpec(_Spec):
    """LDP on an IPv4 interface (ldpConnectedInterface); the device group gets the LDP router."""
    name: str | None = None
    hello_interval: Value = None            # basicHelloInterval
    hold_time: Value = None                 # basicHoldTime
    label_space_id: Value = None            # labelSpaceID
    md5_key: Value = None                   # authentication=md5 + mD5Key
    bfd: Value = None                       # enableBfdRegistration


class LdpRouterSpec(_Spec):
    """The device group's LDP router (ldpBasicRouter), linked to its IPv4 stack by IxNetwork."""
    name: str | None = None
    keepalive: Value = None                 # keepAliveInterval
    keepalive_hold_time: Value = None       # keepAliveHoldTime
    graceful_restart: Value = None          # enableGracefulRestart


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


class SrgbRangeSpec(_Spec):
    start: Value = None                     # first label of the range (startSIDLabel)
    count: Value = None                     # labels in the range (sIDCount)


class IsisSrSpec(_Spec):
    enabled: bool = True                    # SR-MPLS on the emulated routers (enableSR)
    node_prefix: Value = None               # prefix that carries the node SID (nodePrefix)
    node_sid_index: Value = None            # node SID index into the SRGB (sIDIndexLabel)
    srgb: list[SrgbRangeSpec] = Field(default_factory=list)


class IsisRouterSpec(_Spec):
    area_addresses: Value = None
    wide_metric: Value = None
    hostname: Value = None
    te_router_id: Value = None
    sr: IsisSrSpec | None = None            # IS-IS Segment Routing (SR-MPLS)


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
    ldp: LdpSpec | None = None


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
    prefix_sid_index: Value = None          # IS-IS SR: prefix SID index (isisL3PrefixesSrSid)


class LdpFecSpec(_Spec):
    """LDP FEC labels for the prefixes of a pool (ldpFECProperty; needs LDP in the device group)."""
    label_start: Value = None               # labelValue
    label_mode: Value = None                # labelIncrementMode: increment | same


class EvpnPrefixSpec(_Spec):
    """EVPN Type-5 routes for a pool: its connector points at an EVI, and IxNetwork then replaces
    the pool's BGP route property with evpnIPv4PrefixRange / evpnIPv6PrefixRange."""
    evi: str                                # name of an evpn_vxlan / evpn_mpls instance in the pool's device group
    vni: Value = None                       # labelStart: the L3 VNI
    vni_step: Value = None                  # labelMode=increment + labelStep: per prefix of the pool
    next_hop: Value = None                  # setNextHop=manually + ipv4NextHop / ipv6NextHop
    origin: Value = None                    # igp | egp | incomplete
    local_pref: Value = None
    med: Value = None


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
    ldp: LdpFecSpec | None = None           # IPv4 pools; needs LDP in the parent device group
    evpn: EvpnPrefixSpec | None = None      # EVPN Type-5 instead of the route properties above


class MacIpPoolSpec(_Spec):
    """IP addresses inside a MAC pool, advertised with the MACs (EVPN Type-2 MAC/IP routes)."""
    name: str | None = None
    network: Value = None
    prefix_length: Value = None
    count: Value = None                     # addresses per MAC pool (numberOfAddressesAsy)
    address_step: Value = None              # prefixAddrStep


class MacEvpnSpec(_Spec):
    """EVPN Type-2 routes for a MAC pool: its connector points at the EVI and IxNetwork adds
    cMacProperties."""
    evi: str                                # name of an evpn_mpls / evpn_vxlan instance in the pool's device group
    label_start: Value = None               # firstLabelStart
    label_step: Value = None                # labelMode=increment + labelStep
    second_label: Value = None              # secondLabelStart (false: enableSecondLabel=false)
    advertise_ip: Value = None              # advertiseIpv4/6Address (default true with ipv4_pool / ipv6_pool)
    next_hop: Value = None                  # setNextHop=manually + ipv4NextHop / ipv6NextHop
    origin: Value = None                    # igp | egp | incomplete
    local_pref: Value = None
    med: Value = None


class MacPoolSpec(_Spec):
    name: str | None = None
    mac: Value = None                       # first MAC (or a Value pattern)
    count: Value = None                     # MACs per pool (numberOfAddressesAsy)
    prefix_length: Value = None             # default 48
    vlans: list[VlanSpec] = Field(default_factory=list)
    ipv4_pool: MacIpPoolSpec | None = None
    ipv6_pool: MacIpPoolSpec | None = None
    evpn: MacEvpnSpec | None = None


class NetworkGroupSpec(_Spec):
    """IxNetwork allows at most one IPv4 and one IPv6 pool per network group."""
    name: str | None = None
    multiplier: int = Field(1, ge=1)
    ipv4_pool: PoolSpec | None = None
    ipv6_pool: PoolSpec | None = None
    mac_pool: MacPoolSpec | None = None     # instead of ipv4_pool / ipv6_pool (MAC/IP pools go inside it)
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
    ldp_router: LdpRouterSpec | None = None  # created with ethernet.ipv4.ldp (settings only)
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
                    "ldp_router": LdpRouterSpec, "network_groups": NetworkGroupSpec,
                    "device_groups": DeviceGroupSpec},
    "networkGroup": {"ipv4_pool": PoolSpec, "ipv6_pool": PoolSpec, "mac_pool": MacPoolSpec,
                     "device_groups": DeviceGroupSpec},
    "ethernet": {"ipv4": Ipv4Spec, "ipv6": Ipv6Spec, "isis": IsisSpec, "dhcpv4_client": DhcpClientSpec,
                 "dhcpv6_client": DhcpClientSpec},
    "ipv4": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec, "igmp_host": IgmpMldHostSpec,
             "igmp_querier": IgmpMldQuerierSpec, "dhcp_server": DhcpServerSpec, "ldp": LdpSpec},
    "ipv6": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec, "mld_host": IgmpMldHostSpec,
             "mld_querier": IgmpMldQuerierSpec, "dhcp_server": DhcpServerSpec},
    "ipv4Loopback": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec},
    "ipv6Loopback": {"bgp_peers": BgpPeerSpec, "ospf": OspfSpec},
    "bgpIpv4Peer": {"evpn_vxlan": EvpnVxlanSpec, "evpn_mpls": EvpnMplsSpec},
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


@dataclass
class PlanRef:
    """A reference by name to another object, resolved to an xpath when the plan is compiled
    (a pool's or MAC pool's connector.connectedTo -> its EVPN instance)."""
    name: str
    types: tuple[str, ...]
    label: str
    target: Any = None                          # set by the builder: the planned PlanNode or an existing xpath


EVI_TYPES = ("bgpIPv4EvpnVXLAN", "bgpIPv4EvpnEvi")


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
    evis = [k for k in ("evpn_vxlan", "evpn_mpls") if getattr(spec, k) is not None]
    if evis:
        if v6:
            raise PlanError([f"{label}.{evis[0]}: EVPN instances are supported on IPv4 BGP peers"])
        if len(evis) > 1:  # IxNetwork refuses a second EVI of either kind on a peer stack
            raise PlanError([f"{label}: one EVPN instance per BGP peer (evpn_vxlan or evpn_mpls); "
                             "use its count for several EVIs"])
        m["evpn"] = True                    # EVPN capability (AFI 25 / SAFI 70)
        m["filterEvpn"] = True              # and EVPN learned info
        key = evis[0]
        evi = getattr(spec, key)
        if key == "evpn_mpls" or (evi.count or 1) > 1:
            # IxNetwork refuses an MPLS EVI, or any EVI with a multiplier, on a peer without an Ethernet segment
            n.plain["ethernetSegmentsCountV4"] = 1
            es = PlanNode("bgpEthernetSegmentV4", f"{label}.{key}", auto=True, singleton=True)
            if evi.count is not None:
                es.plain["evisCount"] = evi.count
            _set(es.mv, "esiValue", getattr(evi, "esi", None))
            n.children.append(es)
        n.children.append(_evi(evi, "bgpIPv4EvpnEvi" if key == "evpn_mpls" else "bgpIPv4EvpnVXLAN", f"{label}.{key}"))
    _passthrough(n, spec.attributes)
    return n


def _route_targets(kind: str, rts: list[RouteTargetSpec], label: str) -> list[PlanNode]:
    out = []
    for i, rt in enumerate(rts, 1):
        n = PlanNode(kind, f"{label}[{i - 1}]", auto=True, index=i)
        _set(n.mv, "targetType", rt.type)
        if rt.as_number is not None:
            n.mv["targetAs4Number" if rt.type == "as4" else "targetAsNumber"] = rt.as_number
        _set(n.mv, "targetIpAddress", rt.ip)
        _set(n.mv, "targetAssignedNumber", rt.number)
        _passthrough(n, rt.attributes)
        out.append(n)
    return out


def _evi(spec: Union[EvpnVxlanSpec, EvpnMplsSpec], rest_type: str, label: str) -> PlanNode:
    n = PlanNode(rest_type, label)
    if spec.name:
        n.plain["name"] = spec.name
    if spec.count is not None:
        n.plain["multiplier"] = spec.count
    rd_type, rd_as, rd_ip, rd_evi = spec.rd_type, spec.rd_as, spec.rd_ip, spec.rd_evi
    if spec.rd is not None:
        if any(x is not None for x in (rd_type, rd_as, rd_ip, rd_evi)):
            raise PlanError([f"{label}.rd: give either rd or rd_type / rd_as / rd_ip / rd_evi"])
        try:
            admin, rd_evi = _admin_and_number(spec.rd, f"{label}.rd")
        except ValueError as exc:
            raise PlanError([str(exc)]) from None
        if admin.isdigit():
            rd_type, rd_as = ("as4" if int(admin) > 65535 else "as"), int(admin)
        else:
            rd_type, rd_ip = "ip", admin
    m = n.mv
    _set(m, "rdType", rd_type)
    _set(m, "rdASNumber", rd_as)
    if rd_ip is not None:
        m["autoConfigureRdIpAddress"] = False
        m["rdIpAddress"] = rd_ip
    _set(m, "rdEvi", rd_evi)
    if spec.route_targets:
        n.plain["numRtInExportRouteTargetList"] = len(spec.route_targets)
        n.children += _route_targets("bgpExportRouteTargetList", spec.route_targets, f"{label}.route_targets")
    if spec.l3_route_targets:
        m["enableL3vniTargetList"] = True
        n.plain["numRtInL3vniExportRouteTargetList"] = len(spec.l3_route_targets)
        n.children += _route_targets("bgpL3VNIExportRouteTargetList", spec.l3_route_targets,
                                     f"{label}.l3_route_targets")
    if spec.ethernet_tag_id is not None:
        bd = PlanNode("broadcastDomainV4", f"{label}.ethernet_tag_id", auto=True, singleton=True)
        bd.mv["ethernetTagId"] = spec.ethernet_tag_id
        n.children.append(bd)
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
    if not v6 and spec.ldp:  # type: ignore[union-attr]
        n.children.append(_ldp(spec.ldp, f"{label}.ldp"))  # type: ignore[union-attr]
    _passthrough(n, spec.attributes)
    return n


def _ldp(spec: LdpSpec, label: str) -> PlanNode:
    n = PlanNode("ldpConnectedInterface", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "basicHelloInterval", spec.hello_interval)
    _set(n.mv, "basicHoldTime", spec.hold_time)
    _set(n.mv, "labelSpaceID", spec.label_space_id)
    if spec.md5_key is not None:
        n.mv["authentication"] = "md5"
        n.mv["mD5Key"] = spec.md5_key
    _set(n.mv, "enableBfdRegistration", spec.bfd)
    _passthrough(n, spec.attributes)
    return n


def _ldp_router(spec: LdpRouterSpec, label: str) -> PlanNode:
    """Created (not automatic); IxNetwork links it to the device group's IPv4 stack (stackedLayers)
    and then adds ldpFECProperty to the device group's IPv4 pools."""
    n = PlanNode("ldpBasicRouter", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "keepAliveInterval", spec.keepalive)
    _set(n.mv, "keepAliveHoldTime", spec.keepalive_hold_time)
    _set(n.mv, "enableGracefulRestart", spec.graceful_restart)
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


def _vlans(n: PlanNode, vlans: list[VlanSpec], label: str) -> None:
    if not vlans:
        return
    n.plain["vlanCount"] = len(vlans)
    n.mv["enableVlans"] = True
    for i, v in enumerate(vlans, 1):
        vn = PlanNode("vlan", f"{label}.vlans[{i - 1}]", auto=True, index=i)
        _set(vn.mv, "vlanId", v.id)
        _set(vn.mv, "priority", v.priority)
        _set(vn.mv, "tpid", v.tpid)
        _passthrough(vn, v.attributes)
        n.children.append(vn)


def _ethernet(spec: EthernetSpec, label: str) -> PlanNode:
    n = PlanNode("ethernet", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "mac", spec.mac)
    _set(n.mv, "mtu", spec.mtu)
    _vlans(n, spec.vlans, label)
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


def _isis_sr(router: PlanNode, sr: IsisSrSpec, label: str) -> None:
    if not sr.enabled and (sr.node_prefix is not None or sr.node_sid_index is not None or sr.srgb):
        raise PlanError([f"{label}: node_prefix / node_sid_index / srgb need enabled=true"])
    router.plain["enableSR"] = sr.enabled
    _set(router.mv, "nodePrefix", sr.node_prefix)
    _set(router.mv, "sIDIndexLabel", sr.node_sid_index)
    if sr.srgb:
        router.plain["sRGBRangeCount"] = len(sr.srgb)
        for i, g in enumerate(sr.srgb, 1):
            gn = PlanNode("isisSRGBRangeSubObjectsList", f"{label}.srgb[{i - 1}]", auto=True, index=i)
            _set(gn.mv, "startSIDLabel", g.start)
            _set(gn.mv, "sIDCount", g.count)
            _passthrough(gn, g.attributes)
            router.children.append(gn)
    _passthrough(router, sr.attributes)


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
        if spec.prefix_sid_index is not None:
            raise PlanError([f"{label}.prefix_sid_index: IS-IS only"])
    else:
        _set(m, "metric", spec.metric)
        _set(m, "routeOrigin", spec.origin)
        if spec.prefix_sid_index is not None:
            if kind != "isis":
                raise PlanError([f"{label}.prefix_sid_index: IS-IS only"])
            n.plain["noOfSidperPrefix"] = 1  # default 0: no isisL3PrefixesSrSid child exists
            sid = PlanNode("isisL3PrefixesSrSid", f"{label}.prefix_sid_index", auto=True, singleton=True)
            sid.mv["configureSIDIndexLabel"] = True  # the route property's own SID attributes are deprecated in 10.00
            sid.mv["sidIndexLabel"] = spec.prefix_sid_index
            n.children.append(sid)
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
            if spec.evpn is not None:
                raise PlanError([f"{label}.{kind}: a pool connected to an EVPN instance carries only EVPN routes"])
            n.children.append(_route_prop(kind, rp, v6, f"{label}.{kind}"))
    if spec.ldp is not None:
        if v6:
            raise PlanError([f"{label}.ldp: LDP FEC ranges are supported on IPv4 pools"])
        if spec.evpn is not None:
            raise PlanError([f"{label}.ldp: a pool connected to an EVPN instance carries only EVPN routes"])
        fec = PlanNode("ldpFECProperty", f"{label}.ldp", auto=True, index=1, requires="ldp")
        _set(fec.mv, "labelValue", spec.ldp.label_start)
        _set(fec.mv, "labelIncrementMode", spec.ldp.label_mode)
        _passthrough(fec, spec.ldp.attributes)
        n.children.append(fec)
    if spec.evpn is not None:
        n.children += _evpn_prefixes(spec.evpn, v6, f"{label}.evpn")
    _passthrough(n, spec.attributes)
    return n


def _evpn_prefixes(spec: EvpnPrefixSpec, v6: bool, label: str) -> list[PlanNode]:
    """Type-5: the pool's connector points at the EVI, and IxNetwork creates the prefix range
    (verified live: both land from one ImportConfig)."""
    conn = PlanNode("connector", f"{label}.evi", auto=True, singleton=True,
                    plain={"connectedTo": PlanRef(spec.evi, EVI_TYPES, f"{label}.evi")})
    r = PlanNode("evpnIPv6PrefixRange" if v6 else "evpnIPv4PrefixRange", label, auto=True, index=1)
    m = r.mv
    _set(m, "labelStart", spec.vni)
    if spec.vni_step is not None:
        m["labelMode"] = "increment"
        m["labelStep"] = spec.vni_step
    _evpn_route_attrs(m, spec)
    _passthrough(r, spec.attributes)
    return [conn, r]


def _evpn_route_attrs(m: dict[str, Any], spec: Union[EvpnPrefixSpec, MacEvpnSpec]) -> None:
    """Next hop / origin / local pref / MED as named on EVPN route ranges (evpn*PrefixRange, cMacProperties)."""
    if spec.next_hop is not None:
        ip6 = isinstance(spec.next_hop, str) and ":" in spec.next_hop
        m["setNextHop"] = "manually"
        m["setNextHopIpType"] = "ipv6" if ip6 else "ipv4"
        m["ipv6NextHop" if ip6 else "ipv4NextHop"] = spec.next_hop
    _set(m, "origin", spec.origin)
    if spec.local_pref is not None:
        m["enableLocalPreference"] = True
        m["localPreference"] = spec.local_pref
    if spec.med is not None:
        m["enableMultiExitDiscriminator"] = True
        m["multiExitDiscriminator"] = spec.med


def _mac_pool(spec: MacPoolSpec, label: str) -> PlanNode:
    n = PlanNode("macPools", label)
    if spec.name:
        n.plain["name"] = spec.name
    _set(n.mv, "mac", spec.mac)
    _set(n.mv, "numberOfAddressesAsy", spec.count)
    _set(n.mv, "prefixLength", spec.prefix_length)
    _vlans(n, spec.vlans, label)
    for key, v6 in (("ipv4_pool", False), ("ipv6_pool", True)):
        ip = getattr(spec, key)
        if ip is None:
            continue
        pn = PlanNode("ipv6PrefixPools" if v6 else "ipv4PrefixPools", f"{label}.{key}")
        if ip.name:
            pn.plain["name"] = ip.name
        _set(pn.mv, "networkAddress", ip.network)
        _set(pn.mv, "prefixLength", ip.prefix_length)
        _set(pn.mv, "numberOfAddressesAsy", ip.count)
        _set(pn.mv, "prefixAddrStep", ip.address_step)
        _passthrough(pn, ip.attributes)
        n.children.append(pn)
    ev = spec.evpn
    if ev is not None:  # Type-2: connector -> EVI; IxNetwork then creates cMacProperties[1]
        n.children.append(PlanNode("connector", f"{label}.evpn.evi", auto=True, singleton=True,
                                   plain={"connectedTo": PlanRef(ev.evi, EVI_TYPES, f"{label}.evpn.evi")}))
        c = PlanNode("cMacProperties", f"{label}.evpn", auto=True, index=1)
        m = c.mv
        _set(m, "firstLabelStart", ev.label_start)
        if ev.label_step is not None:
            m["labelMode"] = "increment"
            m["labelStep"] = ev.label_step
        if ev.second_label is False:
            m["enableSecondLabel"] = False
        elif ev.second_label is not None:
            m["enableSecondLabel"] = True
            m["secondLabelStart"] = ev.second_label
        advertise = ev.advertise_ip
        if advertise is None and (spec.ipv4_pool or spec.ipv6_pool):
            advertise = True
        if advertise is not None:
            if spec.ipv4_pool or not spec.ipv6_pool:
                m["advertiseIpv4Address"] = advertise
            if spec.ipv6_pool:
                m["advertiseIpv6Address"] = advertise
        _evpn_route_attrs(m, ev)
        _passthrough(c, ev.attributes)
        n.children.append(c)
    elif spec.ipv4_pool or spec.ipv6_pool:
        raise PlanError([f"{label}: MAC/IP pools need evpn (the EVI that advertises them)"])
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
    if spec.mac_pool:
        if spec.ipv4_pool or spec.ipv6_pool:
            raise PlanError([f"{label}.mac_pool: a network group holds a MAC pool or IP pools; put MAC/IP "
                             "addresses in the MAC pool's ipv4_pool / ipv6_pool"])
        n.children.append(_mac_pool(spec.mac_pool, f"{label}.mac_pool"))
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
    has_ldp = bool(spec.ethernet and spec.ethernet.ipv4 and spec.ethernet.ipv4.ldp)
    if spec.ldp_router is not None and not has_ldp:
        raise PlanError([f"{label}.ldp_router: needs ethernet.ipv4.ldp in the same device group"])
    if has_ldp:  # LDP-IF alone does nothing: the device group needs the router as well
        n.children.append(_ldp_router(spec.ldp_router or LdpRouterSpec(), f"{label}.ldp_router"))
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
            if rs.sr is not None:
                _isis_sr(r, rs.sr, f"{label}.{key}.sr")
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
            elif model is EvpnVxlanSpec:
                plans.append(_evi(spec, "bgpIPv4EvpnVXLAN", lbl))
            elif model is EvpnMplsSpec:
                if spec.esi is not None:
                    raise PlanError([f"{lbl}.esi: the peer's Ethernet segment exists already; set its "
                                     "esiValue with set_attributes"])
                plans.append(_evi(spec, "bgpIPv4EvpnEvi", lbl))
            elif model is LdpSpec:
                plans.append(_ldp(spec, lbl))
            elif model is LdpRouterSpec:
                plans.append(_ldp_router(spec, lbl))
            elif model is MacPoolSpec:
                plans.append(_mac_pool(spec, lbl))
            else:
                plans.append(_BUILDERS[model](spec, lbl))
    return plans
