# NGPF hierarchy (what nests where)

```
topology (bound to one or more ports/vports)
└─ deviceGroup (multiplier = number of emulated devices; default 10 — always set it)
   ├─ ethernet (mac, mtu; vlan[1..vlanCount] children always exist)
   │  ├─ ipv4 ─┬─ bgpIpv4Peer, ospfv2, igmpHost (+ igmpMcastIPv4GroupList), igmpQuerier,
   │  │        └─ dhcpv4server (+ dhcp4ServerSessions pool)
   │  ├─ ipv6 ─┬─ bgpIpv6Peer, ospfv3, mldHost (+ mldMcastIPv6GroupList), mldQuerier,
   │  │        └─ dhcpv6server (+ dhcp6ServerSessions pool)
   │  ├─ isisL3            (IS-IS runs on ethernet)
   │  ├─ dhcpv4client      (DHCP clients run on ethernet; they can carry bgp/ospf/igmp on top)
   │  └─ dhcpv6client
   ├─ ipv4Loopback / ipv6Loopback (for protocols over loopbacks)
   ├─ routerData (routerId), ospfv2Router / ospfv3Router / isisL3Router — created automatically
   ├─ networkGroup (routes/prefixes advertised behind the devices)
   │  ├─ ipv4PrefixPools / ipv6PrefixPools
   │  │  └─ bgpIPRouteProperty / bgpV6IPRouteProperty / ospfRouteProperty /
   │  │     ospfv3RouteProperty / isisL3RouteProperty — created automatically for the
   │  │     protocols running in the parent device group
   │  └─ deviceGroup (chained devices behind the network group, e.g. loopbacks)
   └─ deviceGroup (chained)
```

Traffic items reference these objects as endpoints (ipv4/ipv6 stacks, network groups,
dhcp clients, whole topologies) — see the traffic topic once available.
