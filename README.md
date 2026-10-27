# ixnetwork-mcp-server

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent drive a Keysight
IxNetwork **Windows GUI** instance through its REST API (port 11009, session 1):

- inspect ports, topologies, protocol sessions and traffic
- build NGPF topologies and protocol stacks declaratively: Ethernet/VLAN, IPv4/IPv6, BGP
  (including EVPN over VXLAN or MPLS: Type-5 prefix pools and Type-2 MAC/IP pools), LDP,
  OSPFv2/v3, IS-IS (including Segment Routing), IGMP/MLD, DHCPv4/v6 client and server,
  loopbacks, network groups with route pools
- create traffic items between the configured endpoints, run protocols and traffic, and read
  statistics
- save, compare and restore configurations

Every change is applied to the live session shown in the IxNetwork GUI. Changes are validated
before they are sent, verified afterwards, and rolled back when IxNetwork rejects them.
Destructive actions need explicit confirmation and can be disabled per target.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- IxNetwork Windows GUI with the REST API enabled (tested with IxNetwork 10.00). The REST
  schema of other IxNetwork builds is discovered automatically on first use.

## Install

```bash
git clone https://github.com/slava-bondar/ixnetwork-mcp-server.git
cd ixnetwork-mcp-server
uv sync
```

## Configure

Create an inventory outside the repository, e.g. `~/.ixnetwork-mcp/ixnetwork.json`. Each entry
is a *target* (one IxNetwork VM); see [ixnetwork-template.json](ixnetwork-template.json) for all
options.

```json
{
  "lab": {
    "ip": "192.0.2.10",
    "rest_port": 11009,
    "guards": {
      "allow_clear_config": false,
      "allow_traffic_start": true,
      "allow_port_takeover": false
    }
  }
}
```

- String values may reference environment variables, e.g. `"password": "${IXN_PASSWORD}"`.
  The Windows GUI API server needs no credentials by default.
- `guards` control what the agent may do on that target (`read_only`, clearing/loading the
  configuration, starting traffic, taking over chassis ports owned by other sessions, raw imports).
- Destructive actions are confirmed by you: if your MCP client supports elicitation, the server
  asks you directly in the client; otherwise the agent must pass `confirm=true` after asking you in
  chat. `"require_human_confirmation": true` in `guards` refuses the agent's `confirm=true`, so such
  actions only run from clients that can ask you.
- Saved configurations go to `~/.ixnetwork-mcp/configs/<target>/` (override with `config_dir`).

## Register with an MCP client

The server runs over stdio. Replace `/path/to/ixnetwork-mcp-server` with your clone.

Claude Code:

```bash
claude mcp add ixnetwork -- uv --directory /path/to/ixnetwork-mcp-server run python ixmcp.py -f ~/.ixnetwork-mcp/ixnetwork.json -t stdio
```

Claude Desktop (`claude_desktop_config.json`) or GitHub Copilot CLI (`~/.copilot/mcp-config.json`,
add `"type": "local", "tools": ["*"]`):

```json
{
  "mcpServers": {
    "ixnetwork": {
      "command": "uv",
      "args": ["--directory", "/path/to/ixnetwork-mcp-server", "run", "python", "ixmcp.py",
               "-f", "/home/me/.ixnetwork-mcp/ixnetwork.json", "-t", "stdio"]
    }
  }
}
```

Options: `-T/--targets a,b` exposes only some inventory entries, and
`-t streamable-http -H 127.0.0.1 -p 30031` serves HTTP on loopback (no authentication).

## Tools

| Tool | Purpose |
|---|---|
| `list_targets` | configured targets, guard flags, IxNetwork build |
| `get_session_overview` | ports and link state, topology tree with session counts, traffic items, status |
| `list_ports` | vports and chassis ports (owners, link state, LLDP neighbours) |
| `find_nodes` / `get_node` | find objects by type / name / subtree; show one object's values and state |
| `get_protocol_status` | per-protocol session up/down counts, errors, per-device state |
| `get_learned_info` | routes / LSAs / adjacencies learned by BGP, OSPF and IS-IS (inline, or all rows to CSV) |
| `search_node_types` / `describe_node_type` | the IxNetwork object model: attributes, formats, enums, defaults, children, operations |
| `export_config` | scoped export in IxNetwork's import format |
| `apply_config` | declarative build of topologies, protocol stacks and network groups, or additions to existing objects |
| `set_attributes` / `add_node` / `delete_nodes` | change, create or delete individual objects |
| `run_operation` / `import_config_fragment` | IxNetwork operations and raw imports (advanced) |
| `assign_ports` / `unassign_ports` | connect vports to chassis ports |
| `control_protocols` | start / stop / restart down sessions / apply changes on the fly; whole objects or selected devices |
| `configure_traffic_items` / `get_traffic_items` | create, update and read traffic items |
| `describe_packet_headers` / `edit_packet_headers` | packet header templates, an item's packet, and in-place header edits (e.g. MPLS labels) |
| `control_traffic` | generate / apply / start / stop traffic, clear statistics |
| `get_stats` | statistics views with row filters |
| `configure_quick_test` / `control_quick_test` / `get_quick_test_results` | RFC 2544 QuickTests (throughput, frame loss, back-to-back) over traffic items, with the PDF report |
| `control_capture` / `get_capture` | packet capture on named ports: start / stop / release, decoded digest, download capture files |
| `save_config` / `list_saved_configs` / `check_drift` / `load_config` / `new_config` | save, compare and restore configurations |
| `get_guide` | usage guide for the agent: workflow, spec, traffic, hierarchy, values, safety, troubleshooting |

The agent should start with `get_session_overview` and `get_guide('workflow')`.

## Several IxNetwork VMs

One inventory can list any number of VMs. Targets are independent (own connection and lock,
calls run in parallel), and every tool takes a `target` argument. To keep projects apart,
register one server per project and expose only its VM with `--targets`. Chassis are often shared
between VMs: ports owned by another session are never taken over unless the target's
`allow_port_takeover` guard is set and the user confirms.

## Limitations

- Only the IxNetwork Windows GUI REST API is supported (one session per VM).
- BGP communities / AS-path lists and several IGMP/MLD group ranges per host are set through
  the `attributes` passthrough of `apply_config`, not through dedicated keys.
- The HTTP transport has no authentication; bind it to loopback only.
