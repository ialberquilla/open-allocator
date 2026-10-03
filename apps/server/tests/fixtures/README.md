`chat_execute_turn.ndjson` is a `claude -p --output-format stream-json --verbose
--include-partial-messages` turn against this server's `/mcp`: `build-allocation`,
then `execute` returning `plan_required`. Bulky tool payloads are trimmed and
events the bridge ignores are dropped.
