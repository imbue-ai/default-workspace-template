# usage_collector

The workspace's record of model calls. Every model call made in the workspace
is one `model_call` event (the schema in mngr's `imbue.mngr_usage.model_call`)
in an event file; the collector follows those files, keeps an index of them in
SQLite under `data/.apps/usage/usage.db`, and serves it on
`http://127.0.0.1:8795`. Usage data never leaves the workspace except in its
own encrypted backup.

## Where events come from

Every emitter appends to a file, and the collector only reads files, about
once a second, each from where it left off (its place survives restarts):

- **Agents.** Agent harnesses write `model_call` events to each agent's own
  mngr_usage event file (`$MNGR_HOST_DIR/agents/<agent>/events/<source>/usage/events.jsonl`),
  and mngr preserves a destroyed agent's files under `$MNGR_HOST_DIR/preserved/`.
  Lines that are not `model_call` events are read only for the subscription
  limit readings they carry (`rate_limits`); the rest of them is left to
  `mngr usage`.
- **Apps and one-shot calls.** The `workspace_usage` library appends their
  events to `data/.apps/usage/events/<origin>.jsonl`: LiteLLM calls, and
  `claude -p` one-shots read from their session logs.

The files are the source of truth: the database is an index of them, and
deleting it rebuilds it by re-reading them. Events are idempotent by
`event_id`, so a re-read never double-counts. Every event is attributed to this
workspace. A call whose cost is unknown is stored with no cost, never $0; one
with tokens but no cost is priced from mngr_usage's price table and marked
estimated. The database keeps raw events 90 days and per-minute rollups
indefinitely; older raw events stay in the files. It also keeps the id of every
event it has ever counted, so a file read again from its start (a destroyed
agent's preserved copy, a restored backup) never adds an event whose raw row
was already pruned to the rollups a second time.

## API

- `GET /v1/totals?group_by=origin_id,model&since=24h[&until=...][&bucket=hour]`:
  calls, cost and tokens per group. `since` and `until` take a duration
  (`90s`, `30m`, `24h`, `7d`) or an ISO timestamp. Grouping only by origin,
  agent, model and billing mode reads per-minute rollups; any other field reads
  raw events.
- `GET /v1/latency?group_by=model&since=24h[&until=...][&billing_mode=API_KEY,UNKNOWN]`:
  optionally over only the calls whose fields (any groupable one) take the given
  values. p50 and p95 call
  duration (`ended_at` minus `started_at`) per group, from raw events. Calls
  with no duration are left out of the percentiles and counted, and calls
  timed from Claude session logs are counted as approximate.
- `GET /v1/calls?limit=50[&offset=N][&provider=...][&billing_mode=API_KEY,UNKNOWN][&before=<event id>][&after=<sequence>]`:
  stored calls, newest first, filtered by any of origin_kind, origin_id,
  agent_id, model, billing_mode and provider (comma-separated values mean any
  of them), with `total`, how many calls the filters match. `offset` skips
  that many of the newest, for numbered pages; `before` pages back; `after`
  returns the calls stored since a `sequence` a client last saw.
- `GET /v1/turns?limit=20[&offset=N][&provider=...][&billing_mode=...][&since=24h][&until=...]`:
  the turns of the calls the same filters keep, latest first, each summing only
  those calls (calls, failures, tokens, billed cost apart from subscription
  list price, models, source), with `total`, how many turns there are. `since`
  and `until` keep only calls that ended in that window, so a turn is counted
  when any of its calls is. A call with no `turn_id` is a turn of its own; `/v1/calls?turn_id=...` (or
  `?event_id=...` for such a call) lists one turn's calls.
- `GET /v1/limits?since=7d`: the freshest subscription limit reading of every
  window, per source (claude, codex, ...): the share used, when it resets, the
  window's length and when the reading was taken. Readings come from any event
  that carries `rate_limits`, an agent's snapshot or a model_call. A window is
  an account-level counter that every agent on the account reports, so within
  one window (the same reset time) the higher share wins, however recently a
  lower one was stamped; a later reset time starts a new window and wins, and
  without a reset time the freshest wins. Each window also carries its first
  reading (`first_used_percentage`, `first_observed_at`): the lowest share seen
  in it, at the earliest time that share was seen (never a reading dated before
  its window opened), so the rise from there to the
  latest reading is what this workspace's calls in between added (the Usage
  app's estimate of each call's share is calibrated on it).
- `GET /v1/stream`: server-sent events, one `model_call` message per newly
  stored event.
- `GET /health`: `catching_up` is true until a pass has read every event file
  to its end, so totals during a first start or a rebuild are incomplete.

The API listens on 127.0.0.1 only, so who may read usage is the share
gateway's grants: a visitor reaches it only through the Usage app, which the
gateway admits by a workspace-wide grant or a grant on the app.
