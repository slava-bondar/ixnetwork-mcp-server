# Values (multivalues)

Most protocol and address settings are *multivalues*: one value per device in a device group,
described by a pattern. `get_node` shows them in this form, and configuration tools will accept
the same form:

| form | meaning |
|---|---|
| `"10.0.0.1"` | same value on every device |
| `{"start": "10.0.0.1", "step": "0.0.0.1", "direction": "increment"}` | counter |
| `{"list": ["a", "b", "c"]}` | explicit value per device (cycled) |
| `{"string": "Router-{Inc:1,1}"}` | string pattern |
| `{"random_range": {"min": .., "max": .., "step": .., "seed": ..}}` | repeatable random |

Extra keys that may appear:
- `port_step: {"enabled": true, "step": "0.1.0.0"}` — added per port when a topology spans
  several ports (enabled by default for MAC addresses and router ids).
- `nest: [{"owner": "<network group or device group>", "enabled": .., "step": ..}]` — per-level
  steps for device groups chained behind network groups.
- `overlays: [{"index": 3, "value": "x", "count": 1}]` — per-device exceptions.
- A single value with extras is shown as `{"single": "10.0.0.1", "port_step": {...}}`.

All values are strings (`"true"`, `"24"`). `describe_node_type` lists each attribute's `format`
(ipv4, ipv6, mac, decimal, bool, enum, hex, string) and allowed `enums`.
