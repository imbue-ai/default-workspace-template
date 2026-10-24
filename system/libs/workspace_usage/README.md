# workspace_usage

Records the model calls a workspace app makes in the workspace's usage, so
every call is counted and attributed even when it is made outside an agent.

Every call is appended as one `model_call` event (mngr_usage's schema) to an
event file under `data/.apps/usage/events/`, one file per origin
(`<origin_kind>-<origin_id>.jsonl`). The usage collector
(`system/services/usage_collector`) follows those files beside the agents' own
usage event files; recording depends on nothing else running and never fails
the call.

- **Attribution** (`context`): `read_usage_context()` reads the
  `IMBUE_USAGE_CONTEXT` a starter handed down (`origin_kind=CHAT,origin_id=...`).
  Without one, a process under supervisord is attributed to its app (the app
  registry maps the program to its manifest name) or, for any other program,
  to the workspace (`SYSTEM`); anything else is `UNKNOWN`.
- **Direct LiteLLM calls** (`litellm_usage`, the `litellm` extra): call
  `install_litellm_usage_callback()` once at start; every `completion` after
  that is recorded, successes and failures.
- **`claude -p` one-shots** (`claude_one_shot`): run `claude -p` with
  `claude_session_args(session_id)` instead of `--no-session-persistence`, then
  call `record_claude_one_shot(config_dir, session_id, purpose=...)`. It reads
  the calls from the session log with mngr_usage's session-log reader (the one
  the Claude agents' poller runs), records them, and deletes the log.
- **Anything else** (`events`, `emit`): build a `model_call` with
  `build_model_call(...)` and record it with `record_model_call(...)`.
- **Grouping calls by task** (`turn`, optional): an app that wants its calls
  grouped (one group per request, job or conversation turn) wraps the work in
  `with usage_turn(turn_id):`, and every LiteLLM call and `claude -p` one-shot
  recorded inside it carries `attribution.turn_id`. A single LiteLLM call can
  name its own with `metadata={"imbue_turn_id": ...}` instead, which wins over
  the surrounding turn. The turn is a context variable: it follows the code into
  `await`s, but not into a thread pool's workers, so enter `usage_turn` inside
  the function each worker runs (or pass the id as `metadata`). An app that sets
  no turn records its calls without one; the collector groups and filters by
  `turn_id` either way.

It depends on `imbue-mngr-usage` for the contract and the standard-library
reader, but loads none of mngr itself when recording.
