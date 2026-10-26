# Traffic items

`configure_traffic_items(items, mode="upsert"|"create"|"update", dry_run)` creates or updates
traffic items; `get_traffic_items(detail="full")` returns them in the same format for editing.

```json
{
  "name": "A to B v4",
  "traffic_type": "ipv4",
  "bi_directional": false,
  "src_dest_mesh": "oneToOne",
  "endpoint_sets": [
    {"sources": [{"ref": "Site-A IPv4"}], "destinations": [{"ref": "Site-B IPv4"}]}
  ],
  "config_elements": [{
    "frame_rate": {"type": "percentLineRate", "rate": 10},
    "frame_size": {"type": "fixed", "fixed_size": 512},
    "transmission": {"type": "continuous"},
    "stack": [{"protocol": "udp", "fields": [
      {"field_name": "UDP-Source-Port", "single_value": "1234"},
      {"field_name": "UDP-Dest-Port", "value_type": "increment", "start_value": "5000", "step_value": "1", "count": 10}
    ]}]
  }],
  "flow_tracking": {"track_by": ["trackingenabled0", "sourceDestEndpointPair0"]}
}
```

**Endpoints** are objects referenced by name: `{"ref": "<name>"}`.
- IPv4/IPv6 stacks: traffic between those interfaces.
- Network groups: traffic to or from the route pools.
- Device groups and topologies.
- DHCP clients.
- `{"vport": "<port name>"}` for `raw` traffic.
- Multicast: `"multicast_destinations": ["225.0.0.1/0.0.0.1/4"]` (group/step/count) instead of `destinations`, with `bi_directional: false`. The IGMP/MLD hosts on the receiving side join the groups.
- Legacy keys `topology`, `device_group`, `network_group` narrow names by scope.

**A subset of an endpoint's devices** (the GUI's Select Endpoints dialog): add any of these keys to a source or destination. Leave them out to use the whole object.

| Key | Meaning | Default |
|---|---|---|
| `port_start` | 1-based position in the topology's port list, or a port name such as `"Ethernet - 003"` | 1 |
| `port_count` | Number of ports | the remaining ports |
| `device_start` | First device on each port, 1-based | 1 |
| `device_count` | Devices per port | the remaining devices |

- The device range applies on every selected port, so the total is `port_count × device_count`.
- A device group with multiplier 5 on one port has devices 1–5. Out-of-range values are rejected with the valid range.
- One endpoint set can mix whole objects and subsets.
- Example, devices 2–4 of a stack: `{"ref": "Site-A IPv4", "device_start": 2, "device_count": 3}`.

**Settings:**

| Setting | Values |
|---|---|
| `traffic_type` | ipv4, ipv6, ethernetVlan, raw |
| `src_dest_mesh` | oneToOne, manyToMany, fullMesh |
| `frame_rate.type` | percentLineRate, framesPerSecond, bitsPerSecond, interPacketGap |
| `frame_size.type` | fixed (`fixed_size`), increment (`min_size`, `max_size`, `step_size`), random (`min_size`, `max_size`), quadGaussian, weightedPairs |
| `transmission.type` | continuous, fixedFrameCount (`frame_count`), fixedDuration (`duration`), auto / custom / burstFixedDuration |

Burst settings (auto, custom, burstFixedDuration): `min_gap_bytes`, `inter_burst_gap`, `inter_burst_gap_units`, `burst_packet_count`, `repeat_burst`.

**`config_elements`** match the endpoint sets one to one. When there are fewer config elements than endpoint sets, the last one is reused.

**`stack`** lists headers **in packet order**, for example udp, tcp, vxlan, mpls, gre or icmpv4.
- `describe_packet_headers(query=...)` lists the headers.
- `describe_packet_headers(template='udp')` gives each header's field names.
- Repeat a protocol to get more of it. The n-th entry of a protocol is the n-th header of that protocol. Two `mpls` entries make a two-label stack, and a second `vlan` entry adds a second VLAN tag.
- Base Ethernet/VLAN/IP headers already exist, and an entry for one of them only sets its fields. On NGPF endpoints their MAC and IP addresses come from the devices; only fields such as DSCP, TTL, L4 ports and VLAN id can be overridden.
- New headers go to their natural place: MPLS and VLAN after the last Ethernet/VLAN/MPLS header, anything else just before the FCS. They also keep the listed order (never before the previous entry's header), so list `ipv4` before a `gre` + inner `ipv4`.
- Field `value_type` is singleValue (`single_value`), increment/decrement (`start_value`, `step_value`, `count`), random, or valueList (`value_list`). `"auto": true` hands a field back to IxNetwork; MPLS "Bottom of Stack Bit" is automatic by default.
- `get_traffic_items(detail='full')` returns the whole ordered stack (every header but the FCS).

Two MPLS labels on an `ethernetVlan` item (the outer label takes 3 values):
```json
"stack": [
  {"protocol": "mpls", "fields": [{"field_name": "Label Value", "value_type": "valueList", "value_list": ["1001", "2002", "3003"]}]},
  {"protocol": "mpls", "fields": [{"field_name": "Label Value", "single_value": "200"}]}
]
```

**Changing an existing item's packet** in place (endpoints, rate and tracking are left alone), for items built in the GUI too:
1. `describe_packet_headers(traffic_item='<name>')` shows the headers as `mpls#1`, `mpls#2`, … with every field's current value.
2. `edit_packet_headers(traffic_item='<name>', changes=[...])` applies the changes in order and regenerates the item:
   - `{"header": "mpls#2", "fields": [{"field_name": "Label Value", "single_value": "200"}]}` sets fields;
   - `{"add": "mpls", "after": "mpls#2", "fields": [...]}` (or `"before": "ipv4"`) inserts a header;
   - `{"remove": "mpls#3"}` deletes one.

Headers are named `<protocol>#<n>` or by 1-based position. Use `dry_run=true` to validate first. `config_element` (1-based) picks the endpoint set's packet.

**`flow_tracking.track_by`**: always keep `trackingenabled0`. Common options are sourceDestEndpointPair0, flowGroup0, ipv4SourceIp0, ipv4DestIp0, vlanVlanId0 and sourcePort0. Unknown options are rejected with a list of the valid ones.

**Running traffic** (`control_traffic`):
1. Start protocols, so ARP/ND resolves the destination MACs.
2. `generate`, then `apply`.
3. `start`, with `confirm=true` after asking the user. Traffic reaches the device under test.
4. `get_stats('Traffic Item Statistics' | 'Flow Statistics' | 'Port Statistics')`.
5. `stop`.

Until MACs resolve, items show a note about unresolved destination MACs, and `apply` fails with "No Flow Groups". This is normal before protocols are up.

**Delete** traffic items with `delete_nodes(['trafficItem:<name>'], confirm=true)`.
