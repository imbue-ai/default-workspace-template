New service: `usage-collector` keeps the workspace's record of model calls. It follows every usage event file in the workspace, about once a second and from where it left off: each agent's mngr_usage event file, the preserved files of destroyed agents, and the files `workspace_usage` writes for apps and one-shot calls under `data/.apps/usage/events/`. It indexes their `model_call` events in SQLite under `data/.apps/usage/`, with per-minute rollups, and serves cost and token totals grouped by any attribution field, model, billing mode and time bucket (`GET /v1/totals`) and a live stream of new calls (`GET /v1/stream`) on 127.0.0.1:8795.

The event files are the source of truth: the database is an index of them and is rebuilt by re-reading them when it is deleted. Events are idempotent by id and attributed to this workspace; an unknown cost is stored as unknown, never $0, and a token-only event is priced from mngr_usage's price table. Who may read usage is decided by the share gateway's grants on the Usage app, like any other app: the API listens on 127.0.0.1 only.

It also answers the questions the Usage app asks:

- `GET /v1/latency` reports p50 and p95 call duration per group, leaving out and counting calls with no duration, and counting the ones timed from Claude session logs as approximate; it can be filtered to calls whose fields take given values (a provider's calls on several billing modes), since percentiles do not add up across groups.

- `GET /v1/calls` lists stored calls newest first, filtered by origin, agent, model, billing mode or provider (any of several values), with how many calls match, paging by `offset` or back with `before`, and catching up after a reconnect with `after`.

- Totals can be grouped by `cost_provenance`, so a reader can tell which costs were estimated from tokens.

- `GET /health` reports `catching_up` until the collector has read every event file to its end.

- `GET /v1/limits` answers the freshest subscription limit reading of every window per source (Claude's 5-hour and weekly limits, Codex's): the collector now reads the `rate_limits` on agents' snapshot events and on model_calls instead of skipping them.

- The collector keeps the id of every event it has counted, past the 90 days it keeps raw events, so a file read again from its start (a destroyed agent's preserved copy, a restored backup) never counts an old call twice.

The usage collector stores each call's `turn_id` (a new, indexed column; an existing database gains it when the collector starts). Totals can be grouped by `turn_id` (`/v1/totals?group_by=turn_id`), and `/v1/calls` filters by `turn_id`, `session_id` and `run_id` as well as the rollup dimensions, e.g. `?turn_id=<id>` for one turn's calls.

`GET /v1/turns` lists the turns of the calls its filters keep (a call with no turn id is a turn of its own), latest first and paged by `offset`, each with its calls, failures, tokens, billed cost, subscription list price, models and source, and how many turns there are; `since` and `until` keep only the calls that ended in a window, so it can count the turns of a time window; `/v1/calls` also filters by `event_id`, so a turn of one call can be opened.

A usage-collector pass that fails (a full disk, a corrupt store) no longer stops ingestion for good: the pass counts as incomplete, so `/health` reports the store as catching up instead of current, the error is logged once, and the next pass retries.

Within one limit window (the same reset time) the collector keeps the higher share instead of the most recently stamped reading. Every agent on an account reports the account's windows, and an idle agent could keep re-sending an old, lower share with a fresh time, which replaced the real one: the Usage card showed 26% for a 5-hour window at 90%. A later reset time still starts a new window, and a reading with no reset time still falls back to the freshest.

Each limit window in `GET /v1/limits` also carries its first reading, `first_used_percentage` and `first_observed_at`: the lowest share the collector saw in that window, at the earliest time it saw it (a later window starts over, and a reading dated before its window opened, which a writer catching up on old session logs stamps onto an old call, is never one). The rise from it to the latest reading is what this workspace's own calls added to the window, which the Usage app now estimates each call's share from. A database from before gains the two columns when the collector opens it.
