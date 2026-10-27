# Workflow

1. `list_targets` — see which IxNetwork targets exist (omit `target` when there is one).
2. `get_session_overview` — ports (and whether their link is up), topologies with their
   device groups and protocol stacks, traffic items, overall status. Always start here:
   the GUI user may have changed the configuration since you last looked.
3. Inspect objects with `find_nodes` (filter by type/name/subtree) and `get_node` (settable
   values, read-only state, children). `get_node(ref, changed_only=true)` shows what was
   configured rather than every default. `get_protocol_status` shows sessions up/down.
4. `list_ports(include_chassis=true)` shows which chassis ports are free, which belong to other
   users' sessions (never take those unless the user says so), and LLDP neighbors.
5. To learn the object model: `search_node_types('ospf area')`, then
   `describe_node_type('ospfv2', attributes=['area'])` for attribute kinds, formats, enums, defaults.
6. `export_config` on a narrow xpath shows the raw ImportConfig form of an object (slow:
   seconds per topology; never export more than you need).

Building configuration:
- Prefer `apply_config` (read `get_guide('spec')`): describe topologies → device groups →
  ethernet/vlans/ipv4/ipv6 → protocols, network groups with pools and route properties, in one
  spec. Use `dry_run=true` first for large specs. Use `extend` to add to existing objects.
- Change existing values with `set_attributes`; remove objects with `delete_nodes`.

Object-by-object alternative (for things the spec does not cover):
1. `add_node('topology', attributes={'name': 'T1', 'ports': ['Ethernet - 001']})` — a port can
   belong to only one topology (`list_ports` shows which are free).
2. `add_node('deviceGroup', parent='T1', attributes={'name': 'T1 DG1', 'multiplier': 10})` —
   always set the multiplier (number of emulated devices).
3. `add_node('ethernet', parent='T1 DG1', attributes={'mac': {'start': '00:11:00:00:00:01', 'step': '00:00:00:00:00:01'}})`
4. `add_node('ipv4', parent=<ethernet name>, attributes={'address': {'start': '10.0.0.2', 'step': '0.0.0.1'}, 'gatewayIp': '10.0.0.1', 'prefix': 24})`
5. protocols on top, e.g. `add_node('bgpIpv4Peer', parent=<ipv4 name>, attributes={'dutIp': '10.0.0.1', 'type': 'external', 'localAs2Bytes': 65001})`
6. route pools: `add_node('networkGroup', parent='T1 DG1', attributes={'multiplier': 1})`, then
   `add_node('ipv4PrefixPools', parent=<ng>, attributes={'networkAddress': {'start': '100.0.0.0', 'step': '0.0.1.0'}, 'prefixLength': 24, 'numberOfAddresses': 100})`;
   the BGP/OSPF/IS-IS route property below the pool is created automatically — change it with `set_attributes`.
Use `describe_node_type` for attribute names/enums and `set_attributes(..., dry_run=true)` to preview.
Changes are validated before sending, verified after, and rolled back if IxNetwork rejects them.

Running it:
- Ports: `list_ports(include_chassis=true)`, then `assign_ports([{"vport": ..., "location": ...}])`.
  Never take ports owned by another session (other projects share the chassis) unless the user
  explicitly says so.
- Protocols: `control_protocols('start', scope=..., confirm=true)` after the user agreed (packets go
  to the device under test), then `get_protocol_status(only_problems=true)`. To act on some
  devices only, add `devices="1-3,7"` with a device-group scope (start/stop) or a protocol-object
  scope (start/stop/restart_down); the layers below start for the same devices. A protocol stays
  `notStarted` until its IP layer resolves the gateway, which takes 15 s or more when the gateway
  does not answer, so allow `wait_s` for that. Check what the
  protocols learned from the DUT with `get_learned_info(ref)` (BGP routes, OSPF LSAs, IS-IS
  database; `filters`/`save_csv` for large tables). After changing a running configuration:
  `control_protocols('apply_changes', confirm=true)`.

Traffic (read `get_guide('traffic')`):
- `configure_traffic_items` with endpoints referenced by object name, then after protocols are up
  `control_traffic('generate')`, `control_traffic('apply')`, `control_traffic('start', confirm=true)`,
  `get_stats('Traffic Item Statistics')`, `control_traffic('stop')`.

Saving and restoring (only when the user asks):
- "save the config as X" → `save_config(name='X')`; "back up after each change" → call
  `save_config` after every successful change in this conversation.
- "did anything change?" → `check_drift(against='X')`.
- "restore/apply X" → ask, offer to save the current state first, then
  `load_config('X', confirm=true)` (`method='json'` + `port_map` for another VM/chassis).

Several IxNetwork VMs: each is a `target` (see `list_targets`). When more than one is
configured, pass `target` on every call. Targets are independent — nothing is shared or
synchronized between them.

Conventions:
- Objects are identified by their **name** (IxNetwork guarantees names are unique within the
  configuration) or by **xpath** (`/topology[1]/deviceGroup[2]`). Xpath positions shift when
  objects are deleted — re-read them instead of reusing old ones.
- Device-level values (addresses, AS numbers, VLAN ids...) are *multivalues*: one value per
  device, expressed as a pattern (single value, counter start/step, list...).
- Saving/restoring configurations happens only when the user asks for it.
