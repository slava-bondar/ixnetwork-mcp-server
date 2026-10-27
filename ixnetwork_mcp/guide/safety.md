# Safety

- There is exactly one IxNetwork session and it is the one the user is looking at in the GUI.
  Every change is live. Do not delete or overwrite objects you did not create unless the user
  asked for it.
- Destructive actions (deleting objects, loading or clearing the configuration, releasing ports,
  starting traffic) require explicit user intent, `confirm=true`, and may be disabled by the
  target's guard flags (`list_targets` shows them). A `GUARD_BLOCKED` error means the inventory
  forbids the action — tell the user; do not try to work around it.
- `delete_nodes` and state-changing `run_operation` calls return `CONFIRM_REQUIRED` unless
  `confirm=true`: ask the user first. Port location changes go through the port tools only.
- If the MCP client supports elicitation, the server itself asks the user to confirm destructive
  actions (then `confirm` is ignored). `CONFIRM_DECLINED` means the user said no: do not retry
  unless they ask again. Targets with `guards.require_human_confirmation` only accept that
  confirmation, never `confirm=true`.
- Nothing is saved automatically. Before a destructive action, ask the user whether to save the
  current configuration first.
- The chassis ports may be cabled to a device under test: starting protocols or traffic sends
  real packets to it. Only do so when the user asks.
