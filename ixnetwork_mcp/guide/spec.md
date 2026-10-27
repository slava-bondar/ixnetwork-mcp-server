# apply_config spec

`apply_config(spec)` builds whole topologies, or adds objects under existing ones, in one
validated step. Nothing is written if any part of the spec is wrong. Every new subtree is
verified afterwards and removed again if IxNetwork did not build it exactly.

```json
{
  "topologies": [
    {
      "name": "Site-A", "ports": ["Ethernet - 001"],
      "device_groups": [
        {
          "name": "Site-A PE", "multiplier": 2, "router_id": {"start": "1.1.1.1", "step": "0.0.0.1"},
          "ethernet": {
            "name": "Site-A ETH", "mac": {"start": "00:10:00:00:00:01", "step": "00:00:00:00:00:01"},
            "vlans": [{"id": {"start": 100, "step": 1}}],
            "ipv4": {
              "name": "Site-A IPv4", "address": {"start": "10.0.0.2", "step": "0.0.0.1"},
              "gateway": "10.0.0.1", "prefix": 24,
              "bgp_peers": [{"name": "Site-A BGP", "dut_ip": "10.0.0.1", "type": "external", "local_as": 65001}],
              "ospf": {"name": "Site-A OSPF", "area_id": 0, "network_type": "pointtopoint"}
            }
          },
          "network_groups": [{
            "name": "Site-A routes", "multiplier": 1,
            "ipv4_pool": {"name": "Site-A pool", "network": {"start": "100.1.0.0", "step": "0.1.0.0"},
                          "prefix_length": 24, "count": 50, "bgp": {"local_pref": 200}}
          }]
        }
      ]
    }
  ],
  "extend": [
    {"parent": "Existing IPv4 1", "add": {"bgp_peers": [{"name": "New BGP", "dut_ip": "20.0.0.1", "type": "internal", "local_as": 65000}]}}
  ]
}
```

Rules:
- **Names** must be unique in the whole configuration. Give every object a name, because names are how you refer to objects later.
- `multiplier` is the number of emulated devices in a device group. Always set it: IxNetwork's own default is 10.
- A **port** can belong to only one topology. `list_ports` shows which ports are free.
- Per-device values accept the Value forms in `get_guide('values')`: a plain value, a `{start, step}` counter, `{list}`, and so on.
- `router_id` needs BGP or OSPF in the same device group. A pool's `bgp`, `bgp_v6`, `ospf` or `isis` block needs that protocol in the pool's parent device group, because IxNetwork creates those objects automatically.
- An IGMP/MLD **host** and **querier** cannot share one IP stack. Put them in separate device groups.
- A BGP peer has at most **one EVPN instance**, `evpn_vxlan` or `evpn_mpls`. Its `count` gives several EVIs per peer.
- `ipv4.ldp` also creates the device group's LDP router, which IxNetwork links to the IPv4 stack. A pool's `ldp` block needs LDP in its parent device group.
- Every object also takes `attributes: {restName: value}` for settings without a friendly key. `describe_node_type` lists them.
- `extend[].add` keys by parent type:

| parent type | keys |
|---|---|
| topology | device_groups |
| deviceGroup | ethernet, loopback_v4, loopback_v6, ldp_router, network_groups, device_groups |
| networkGroup | ipv4_pool, ipv6_pool, mac_pool, device_groups |
| ethernet | ipv4, ipv6, isis, dhcpv4_client, dhcpv6_client |
| ipv4 | bgp_peers, ospf, igmp_host, igmp_querier, dhcp_server, ldp (the device group needs an LDP router: existing, or `ldp_router` added in the same call) |
| ipv6 | bgp_peers, ospf, mld_host, mld_querier, dhcp_server |
| ipv4Loopback / ipv6Loopback | bgp_peers, ospf |
| bgpIpv4Peer | evpn_vxlan, evpn_mpls (also set the peer's `evpn` capability with `set_attributes`; for `evpn_mpls` or a `count` above 1 also its ethernetSegmentsCountV4 to 1) |

## Field reference (friendly key → IxNetwork attribute)

**topology:** `name`, `ports` (vport names), `device_groups`.

**device group** (deviceGroup):
- `name`, `multiplier`, `enabled` (enabled)
- `router_id` (routerData.routerId)
- `ethernet`, `loopback_v4`, `loopback_v6`
- `ospfv2_router`, `ospfv3_router`: DG-level router objects; `attributes` only
- `isis_router`: `area_addresses` (areaAddresses), `wide_metric` (enableWideMetric), `hostname` (enableHostName + hostName), `te_router_id` (enableTE + tERouterId), `sr` (Segment Routing, below)
  - `sr`: `enabled` (enableSR, default true), `node_prefix` (nodePrefix: the prefix that carries the node SID), `node_sid_index` (sIDIndexLabel: index into the SRGB), `srgb`: a list of label ranges `{start, count}` (sRGBRangeCount + isisSRGBRangeSubObjectsList: startSIDLabel, sIDCount). Other SR settings (algorithmsrmpls, flags) via `attributes`.
  - Example: `"isis_router": {"area_addresses": "49.0001", "sr": {"node_sid_index": {"start": 100, "step": 1}, "srgb": [{"start": 16000, "count": 8000}]}}`
- `ldp_router`: settings of the LDP router that `ethernet.ipv4.ldp` creates (below)
- `network_groups`, `device_groups` (chained)

**ethernet:**
- `name`, `mac`, `mtu`
- `vlans`: [{`id` (vlanId), `priority`, `tpid` (ethertype8100 …)}]; sets enableVlans and vlanCount
- `ipv4`, `ipv6`, `isis`, `dhcpv4_client`, `dhcpv6_client`

**ipv4 / ipv6:**
- `name`, `address`, `prefix`, `gateway` (gatewayIp), `resolve_gateway` (resolveGateway), `manual_gateway_mac` (IPv4 only)
- `bgp_peers`, `ospf`, `dhcp_server`
- `igmp_host` / `igmp_querier` on IPv4; `mld_host` / `mld_querier` on IPv6
- `ldp` on IPv4 (below)

**ldp** (ldpConnectedInterface on IPv4) and **ldp_router** (the device group's ldpBasicRouter):
- `ldp`: `name`, `hello_interval` (basicHelloInterval), `hold_time` (basicHoldTime), `label_space_id` (labelSpaceID), `md5_key` (authentication=md5 + mD5Key), `bfd` (enableBfdRegistration)
- `ldp_router`: `name`, `keepalive` (keepAliveInterval), `keepalive_hold_time` (keepAliveHoldTime), `graceful_restart` (enableGracefulRestart)
- Example: `"ipv4": {..., "ospf": {...}, "ldp": {"hello_interval": 5}}`; the loopback pool behind it can carry `"ldp": {"label_start": {"start": 16, "step": 1}}`

**bgp_peers[]** (bgpIpv4Peer / bgpIpv6Peer):
- `name`, `dut_ip` (dutIp), `type` (internal | external)
- `local_as`: localAs2Bytes, or enable4ByteAs + localAs4Bytes when above 65535. `local_as_4byte` forces 4-byte AS for patterns.
- `bgp_id` (bgpId), `bgp_id_same_as_router_id`
- `hold_time` (holdTimer), `keepalive` (keepaliveTimer)
- `md5_key` (authentication=md5 + md5Key)
- `graceful_restart` (enableGracefulRestart), `bfd` (enableBfdRegistration; IPv4 only)
- `ipv4_unicast` (capabilityIpV4Unicast), `ipv6_unicast` (capabilityIpV6Unicast)
- `evpn_vxlan` / `evpn_mpls`: the peer's EVPN instance over VXLAN or MPLS (IPv4 peers; one per peer; below). Sets the peer's `evpn` capability and filterEvpn.

**evpn_vxlan** (bgpIPv4EvpnVXLAN) / **evpn_mpls** (bgpIPv4EvpnEvi):
- `name`; refer to it from a pool's or MAC pool's `evpn.evi`
- `count`: EVIs per peer (the EVI's multiplier); per-EVI values then take patterns, e.g. `"rd_evi": {"list": [10001, 10002]}`
- IxNetwork needs an Ethernet segment on the peer for `evpn_mpls`, and for `evpn_vxlan` with `count` above 1: the peer gets one (ethernetSegmentsCountV4=1, evisCount = `count`). `esi` (`evpn_mpls`) sets its esiValue (default 0, single-homed)
- `rd`: shorthand `"65001:100"` (AS) or `"192.0.2.1:100"` (IP). Or the fields `rd_type` (as | ip | as4), `rd_as` (rdASNumber), `rd_ip` (rdIpAddress; turns off autoConfigureRdIpAddress), `rd_evi` (rdEvi, e.g. `{"start": 100, "step": 1}`)
- `route_targets`: export RTs (the import RTs are the same): `["65001:100"]`, or objects with `type`, `as_number`, `ip`, `number` (targetType, targetAsNumber / targetAs4Number, targetIpAddress, targetAssignedNumber)
- `l3_route_targets`: RTs of the L3 VNI (Type-5 routes; enableL3vniTargetList), same forms
- `ethernet_tag_id` (broadcastDomainV4.ethernetTagId)

**ospf** (ospfv2 on IPv4 / ospfv3 on IPv6):
- `name`
- `area_id`: a number sets areaId; a dotted quad sets areaIdIp (with typeAreaId=ip on v2)
- `network_type` (v2: pointtopoint | broadcast | pointtomultipoint; v3: pointtopoint | broadcast)
- `hello_interval`, `dead_interval`, `priority`
- `metric` (v2 metric / v3 linkMetric)
- `auth` (none | password | md5; v2 only), with `auth_key` and `auth_key_id`
- `instance_id` (v3 only)

**isis** (isisL3 on ethernet):
- `name`, `network_type` (pointpoint | broadcast), `level` (levelType: level1 | level2 | l1l2), `metric` (interfaceMetric)
- `hello_interval`, `dead_interval` (applied to both levels)

**igmp_host / mld_host:**
- `name`, `version` (IGMP 1–3, MLD 1–2)
- `groups`: {`start` (startMcastAddr), `count` (mcastAddrCnt), `step` (mcastAddrIncr), `source_mode` (include | exclude)}

**igmp_querier / mld_querier:** `name`, `version`, `query_interval` (generalQueryInterval), `robustness` (robustnessVariable).

**dhcpv4_client / dhcpv6_client:** `name`, `broadcast` (v4: dhcp4Broadcast), `ia_type` (v6: iana | iata | iapd | iana_iapd), `duid_type` (v6).

**dhcp_server:** `name`, `pool`:
- `start` (ipAddress), `size` (poolSize), `prefix` (ipPrefix), `lease_time` (defaultLeaseTime)
- v4 only: `gateway` (ipGateway), `dns1`, `dns2`
- v6 only: `ia_type` (iaType)

**loopback_v4 / loopback_v6:** `name`, `address`, `prefix`, `bgp_peers`, `ospf`.

**network_groups[]:** `name`, `multiplier`, `ipv4_pool`, `ipv6_pool` (at most one of each per network group), `mac_pool` (instead of IP pools), `device_groups` (chained behind the group, e.g. loopbacks).

**ipv4_pool / ipv6_pool** (ipv4PrefixPools / ipv6PrefixPools):
- `name`, `network` (networkAddress), `prefix_length`, `count` (numberOfAddressesAsy), `address_step` (prefixAddrStep)
- route properties:
  - `bgp` (bgpIPRouteProperty; needs an IPv4 BGP peer)
  - `bgp_v6` (bgpV6IPRouteProperty; needs an IPv6 BGP peer)
  - `ospf` (ospfRouteProperty, or ospfv3RouteProperty on v6 pools)
  - `isis` (isisL3RouteProperty)
  - `ldp` (ldpFECProperty, IPv4 pools; needs LDP in the device group): `label_start` (labelValue), `label_mode` (labelIncrementMode: increment | same)
- `evpn`: EVPN Type-5 routes instead of the route properties (below)

**evpn** (on ipv4_pool / ipv6_pool): the pool's connector points at the EVI, and IxNetwork replaces its BGP route property with evpnIPv4PrefixRange / evpnIPv6PrefixRange.
- `evi`: name of an `evpn_vxlan` / `evpn_mpls` instance in the pool's device group (or an existing one there, with `extend`)
- `vni` (labelStart: the L3 VNI; the label with `evpn_mpls`), `vni_step` (labelMode=increment + labelStep, per prefix)
- `next_hop` (setNextHop=manually + ipv4NextHop / ipv6NextHop), `origin` (igp | egp | incomplete), `local_pref`, `med`
- Example: `"ipv4_pool": {"network": "203.0.113.0", "count": 10, "evpn": {"evi": "PE1 EVI", "vni": 5000}}` with `"evpn_vxlan": {"name": "PE1 EVI", "rd": "65001:100", "route_targets": ["65001:100"], "l3_route_targets": ["65001:5000"]}` on the device group's BGP peer

**route property:**
- `next_hop` (nextHopType=manual + ipv4NextHop/ipv6NextHop; BGP)
- `origin` (BGP: igp | egp | incomplete; OSPF/IS-IS: routeOrigin)
- `local_pref` (enableLocalPreference + localPreference; BGP)
- `med` (enableMultiExitDiscriminator + multiExitDiscriminator; BGP)
- `metric` (OSPF/IS-IS)
- `prefix_sid_index` (IS-IS SR: isisL3PrefixesSrSid configureSIDIndexLabel=true + sidIndexLabel, e.g. `{"start": 1000, "step": 1}`; the IS-IS router needs `sr`)

**mac_pool** (macPools): MACs behind the device group; with `evpn`, advertised as EVPN Type-2 routes.
- `name`, `mac` (first MAC or a pattern), `count` (numberOfAddressesAsy), `prefix_length` (default 48), `vlans` (as on ethernet)
- `ipv4_pool` / `ipv6_pool`: IP addresses inside the MAC pool, advertised with the MACs (MAC/IP routes): `name`, `network`, `prefix_length`, `count`, `address_step`
- `evpn` (cMacProperties; the MAC pool's connector points at the EVI):
  - `evi`: name of an `evpn_mpls` / `evpn_vxlan` instance in the pool's device group
  - `label_start` (firstLabelStart), `label_step` (labelMode=increment + labelStep), `second_label` (secondLabelStart; `false` turns enableSecondLabel off)
  - `advertise_ip` (advertiseIpv4Address / advertiseIpv6Address; default true when the MAC pool has IP pools)
  - `next_hop`, `origin`, `local_pref`, `med` (as on Type-5 pools)

**Example: EVPN over MPLS, P and PE routers** (OSPF + LDP core, PEs chained behind the P routers' loopback pool, MAC/IP routes):

```json
{"name": "P", "multiplier": 2, "router_id": {"start": "192.0.2.1", "step": "0.0.0.1"},
 "ethernet": {"vlans": [{"id": {"start": 1001, "step": 1}}],
   "ipv4": {"address": {"start": "198.51.100.2", "step": "0.0.1.0"}, "gateway": {"start": "198.51.100.1", "step": "0.0.1.0"},
            "prefix": 24, "ospf": {"network_type": "broadcast"}, "ldp": {}}},
 "network_groups": [{"name": "PE loopbacks",
   "ipv4_pool": {"network": {"start": "203.0.113.1", "step": "0.0.0.2"}, "prefix_length": 32, "count": 2,
                 "ospf": {"origin": "anotherarea"}, "ldp": {"label_start": {"start": 16, "step": 1}}},
   "device_groups": [{"name": "PE", "multiplier": 2,
     "loopback_v4": {"address": {"start": "203.0.113.1", "step": "0.0.0.1"}, "prefix": 32,
       "bgp_peers": [{"dut_ip": "192.0.2.100", "type": "internal", "local_as": 100,
                      "evpn_mpls": {"name": "PE EVI", "count": 2, "rd_type": "ip", "rd_evi": {"list": [10001, 10002]},
                                    "route_targets": [{"type": "as", "as_number": 100, "number": {"list": [1, 2]}}]}}]},
     "network_groups": [{"name": "CE MACs", "mac_pool": {"mac": "00:00:5e:00:53:01", "count": 5,
       "ipv4_pool": {"network": "198.51.100.200", "prefix_length": 32, "count": 5},
       "evpn": {"evi": "PE EVI"}}}]}]}]}
```
