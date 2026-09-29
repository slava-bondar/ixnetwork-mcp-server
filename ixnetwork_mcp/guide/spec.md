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
- Every object also takes `attributes: {restName: value}` for settings without a friendly key. `describe_node_type` lists them.
- `extend[].add` keys by parent type:

| parent type | keys |
|---|---|
| topology | device_groups |
| deviceGroup | ethernet, loopback_v4, loopback_v6, network_groups, device_groups |
| networkGroup | ipv4_pool, ipv6_pool, device_groups |
| ethernet | ipv4, ipv6, isis, dhcpv4_client, dhcpv6_client |
| ipv4 | bgp_peers, ospf, igmp_host, igmp_querier, dhcp_server |
| ipv6 | bgp_peers, ospf, mld_host, mld_querier, dhcp_server |
| ipv4Loopback / ipv6Loopback | bgp_peers, ospf |

## Field reference (friendly key → IxNetwork attribute)

**topology:** `name`, `ports` (vport names), `device_groups`.

**device group** (deviceGroup):
- `name`, `multiplier`, `enabled` (enabled)
- `router_id` (routerData.routerId)
- `ethernet`, `loopback_v4`, `loopback_v6`
- `ospfv2_router`, `ospfv3_router`: DG-level router objects; `attributes` only
- `isis_router`: `area_addresses` (areaAddresses), `wide_metric` (enableWideMetric), `hostname` (enableHostName + hostName), `te_router_id` (enableTE + tERouterId)
- `network_groups`, `device_groups` (chained)

**ethernet:**
- `name`, `mac`, `mtu`
- `vlans`: [{`id` (vlanId), `priority`, `tpid` (ethertype8100 …)}]; sets enableVlans and vlanCount
- `ipv4`, `ipv6`, `isis`, `dhcpv4_client`, `dhcpv6_client`

**ipv4 / ipv6:**
- `name`, `address`, `prefix`, `gateway` (gatewayIp), `resolve_gateway` (resolveGateway), `manual_gateway_mac` (IPv4 only)
- `bgp_peers`, `ospf`, `dhcp_server`
- `igmp_host` / `igmp_querier` on IPv4; `mld_host` / `mld_querier` on IPv6

**bgp_peers[]** (bgpIpv4Peer / bgpIpv6Peer):
- `name`, `dut_ip` (dutIp), `type` (internal | external)
- `local_as`: localAs2Bytes, or enable4ByteAs + localAs4Bytes when above 65535. `local_as_4byte` forces 4-byte AS for patterns.
- `bgp_id` (bgpId), `bgp_id_same_as_router_id`
- `hold_time` (holdTimer), `keepalive` (keepaliveTimer)
- `md5_key` (authentication=md5 + md5Key)
- `graceful_restart` (enableGracefulRestart), `bfd` (enableBfdRegistration; IPv4 only)
- `ipv4_unicast` (capabilityIpV4Unicast), `ipv6_unicast` (capabilityIpV6Unicast)

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

**network_groups[]:** `name`, `multiplier`, `ipv4_pool`, `ipv6_pool` (at most one of each per network group), `device_groups` (chained behind the group, e.g. loopbacks).

**ipv4_pool / ipv6_pool** (ipv4PrefixPools / ipv6PrefixPools):
- `name`, `network` (networkAddress), `prefix_length`, `count` (numberOfAddressesAsy), `address_step` (prefixAddrStep)
- route properties:
  - `bgp` (bgpIPRouteProperty; needs an IPv4 BGP peer)
  - `bgp_v6` (bgpV6IPRouteProperty; needs an IPv6 BGP peer)
  - `ospf` (ospfRouteProperty, or ospfv3RouteProperty on v6 pools)
  - `isis` (isisL3RouteProperty)

**route property:**
- `next_hop` (nextHopType=manual + ipv4NextHop/ipv6NextHop; BGP)
- `origin` (BGP: igp | egp | incomplete; OSPF/IS-IS: routeOrigin)
- `local_pref` (enableLocalPreference + localPreference; BGP)
- `med` (enableMultiExitDiscriminator + multiExitDiscriminator; BGP)
- `metric` (OSPF/IS-IS)
