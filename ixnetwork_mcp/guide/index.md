# IxNetwork MCP guide

This server drives a Keysight IxNetwork **Windows GUI** instance over its REST API
(session 1). Every change is applied to the live session the user sees in the GUI.

Topics (read with `get_guide(topic)`):
- `workflow` — how to approach a task; which tools to call in which order
- `spec` — the apply_config spec: every field, rules, and a full example
- `traffic` — traffic items, endpoints, packet headers, running traffic and statistics
- `hierarchy` — how IxNetwork NGPF objects nest (topology → device group → stack)
- `safety` — what is guarded, what needs `confirm`, what never to do
- `troubleshooting` — error codes and what to do about them
- `values` — how per-device values (multivalues) are shown and written

Tools available now:
- look around: `list_targets`, `get_session_overview`, `list_ports`, `get_protocol_status`
- find and inspect objects: `find_nodes`, `get_node`, `export_config`
- learn the object model: `search_node_types`, `describe_node_type`, `get_guide`
- build configuration declaratively: `apply_config` (whole topologies, or additions via `extend`)
- change configuration piece by piece: `set_attributes`, `add_node`, `delete_nodes`, `run_operation`,
  `import_config_fragment` (advanced)
- ports and protocols: `assign_ports`, `unassign_ports`, `control_protocols`
- traffic: `configure_traffic_items`, `get_traffic_items`, `describe_packet_headers`,
  `control_traffic`, `get_stats`
- save / compare / restore (only when the user asks): `save_config`, `list_saved_configs`,
  `check_drift`, `load_config`, `new_config`

