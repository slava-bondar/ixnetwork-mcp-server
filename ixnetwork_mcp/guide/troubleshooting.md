# Troubleshooting (error codes)

Errors come back as JSON: `{"code", "message", "hint", ...}`.

| code | meaning | what to do |
|---|---|---|
| NOT_FOUND | unknown target / object / topic | check `candidates`, re-read the overview |
| AMBIGUOUS_REF | a name/ref matched several objects | pick one of `candidates` (use its xpath) |
| VALIDATION | bad input (e.g. unsupported ref, bad value) | fix the input as the hint says |
| GUARD_BLOCKED | the inventory forbids this action on this target | tell the user |
| CONFIRM_REQUIRED | destructive action called without `confirm=true` | ask the user, then repeat with `confirm=true` |
| IXN_UNREACHABLE | REST API not reachable | the IxNetwork GUI may be closed or restarting |
| IXN_AUTH | credentials rejected | inventory credentials are wrong |
| IXN_BUSY | IxNetwork reported API contention (HTTP 409) | wait for the running operation, retry |
| IXN_DEADLINE | the call did not finish within the tool deadline | check for a long operation in the GUI, retry later |
| IXN_ERROR | IxNetwork rejected the request | read the message; it usually names the bad attribute/value |
| PROTOCOLS_RUNNING | change needs protocols stopped or apply-on-the-fly | stop protocols or apply changes on the fly |
