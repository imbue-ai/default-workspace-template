# Codex model bar: read `minds_model_state.json`, switch to EAGER_THEN_RECONCILE

Status: proposed. Owner: model bar. Touches `system/apps/system_interface`
(codex harness) only.

## Scope (deliberately minimal)

Two changes, nothing else:

1. **Live reads** — `CodexModelResolver` reads the effective model from
   `minds_model_state.json` instead of tailing the rollout for
   `thread_settings_applied`.
2. **Switch mode** — flip the codex catalog from `ON_CHANGE` to
   `EAGER_THEN_RECONCILE`, so the chip moves on click and reconciles from the
   pushed live choice.

The rapid-switch delivery stall (a separate, backend send-path issue) is **not**
addressed here — see "Known limitation" for why that's acceptable and what the
fix would be if it ever bites.

## Why this is a fix, not just a refactor (live-verified)

Driven against the patched codex 0.146.0 that `setup_system.sh` installs, in a
throwaway `CODEX_HOME`:

- **The rollout has no `thread_settings_applied` event.** Model/effort now ride a
  `turn_context` payload; `service_tier` isn't in the rollout at all. So the
  current `read_live` (which greps for `event_msg` -> `thread_settings_applied`)
  returns `None` on every read, and the codex model bar silently shows only the
  `config.toml` launch guess — mid-session `/model` / `/fast` changes never
  surface. The reroute repairs a currently-dead live read.
- **`minds_model_state.json` exists at session open**, before the first prompt,
  holding the launch values:
  `{"model":"gpt-5.6-terra","reasoning_effort":"medium","service_tier":"default"}`.
  (All three fields were present in practice, including `service_tier:"default"`.)
- **It updates atomically in well under 100 ms** on `/model gpt-5.5 high`
  (-> `reasoning_effort:"high"`, `model:"gpt-5.5"`) and on `/fast on`
  (-> `service_tier:"priority"`), including *before the first turn exists*, when
  there is no rollout to read at all.

That last point is why EAGER_THEN_RECONCILE is safe to turn on now: the reconcile
source updates near-instantly and reliably, so the optimistic chip snaps to truth
almost immediately rather than hanging on the 5-minute fallback.

## The file

The patched codex writes, atomically, on every model/effort change and every
service-tier change:

```
<agent_state_dir>/plugin/codex/home/minds_model_state.json     # == $CODEX_HOME/minds_model_state.json
```

```json
{"model":"gpt-5.5","reasoning_effort":"high","service_tier":"priority"}
```

Covers framework-initiated changes too (session configure/resume, server-pushed
thread-settings, thread switches, the out-of-usage "switch model" prompt,
fast-mode toggles), per the codex-in-minds README.

Field mapping to `ModelIdentity` (same rules the old rollout reader used, so
`match_option` downstream is unchanged):

| file field         | identity field | rule                                              |
|--------------------|----------------|---------------------------------------------------|
| `model`            | `model_id`     | verbatim; missing/empty -> whole read is `None`   |
| `reasoning_effort` | `effort`       | `parse_effort_level` (non-empty string, else None)|
| `service_tier`     | `fast`         | `== "priority"` -> True, else False               |

This is the codex twin of pi's `PiModelResolver` reading `pi_model_state.json`.

## Change 1 — `harnesses/codex/model.py`

- **`read_live`**: `read_json_dict(<codex_home>/minds_model_state.json)`, map per
  the table, return `None` when `model` is absent/empty. Delete the rollout cursor
  machinery (`_consume_new_settings`, `_current_rollout`, `_offset`,
  `_last_settings`, the `threading.Lock`) and the
  `thread_settings`/`_identity_from_thread_settings` helpers. The resolver no
  longer imports `resolve_active_rollout_path` / `codex_sessions_dir`. ~90 lines
  go.
- **`watched_paths`**: return `(<codex_home>/minds_model_state.json,)`. A file is
  watched via its parent dir, so this is enough and is tighter than the old
  recursive `sessions/` watch. Add a `codex_model_state_path(agent_state_dir)`
  helper so the relative path lives in one place.
- **`guess_from_launch`**: unchanged (still reads `config.toml` for the pre-launch
  window before the agent's `CODEX_HOME` exists; `merge_identities` still fills a
  missing effort from it).
- **No dual reader / fallback.** The rollout path is dead on the installed binary
  anyway; keeping it would be a second, disagreeing source of truth. If an
  unpatched codex is ever run, `read_live` returns `None` and the bar falls back
  to the launch guess — a correct, honest degrade. Note this in the docstring.

## Change 2 — the catalog switch mode

In `CODEX_CATALOG` (`harnesses/codex/model.py`), set
`switch_mode=SwitchMode.EAGER_THEN_RECONCILE`.

How it behaves (from `frontend/src/models/ModelSettings.ts`): on a pick the chip
shows it immediately as a `pending` overlay and POSTs the switch; the overlay
clears the instant the pushed live choice matches the pick
(`getEffectiveChoice`). If it never matches, `schedulePendingTimeout` drops the
overlay after `PENDING_TIMEOUT_MS = 5 * 60 * 1000` (**5 minutes**) and the chip
reverts to whatever is live. So: instant on click, snaps to truth on reconcile
(sub-second here), 5-minute revert only as a failure fallback. `switch()` itself
is unchanged — it still sends `/model` / `/fast`.

## Known limitation (why the rapid-switch stall is out of scope)

Firing several switches in quick succession is fast the first time, then lags a
few seconds each after. Root cause (verified by reading the send path, not the
binary): a `/model` / `/fast` is delivered as a tmux slash command through
`send_message`, which holds an **exclusive per-agent `flock`** across a
submission-confirmation window; codex emits none of the evidence mngr's probes
watch for a settings command, so the window runs its full RELAXED 15 s before
releasing the lock, and the next switch's delivery queues behind it.

With EAGER_THEN_RECONCILE this is **hidden from the chip** — each pick shows
instantly and the final state reconciles correctly (the overlay only clears on an
exact match, so it never flips to an intermediate model). The residual cost: the
*actual* model codex is running can lag the chip by a few seconds during a
flurry, so a prompt fired mid-flurry could run on the not-yet-applied model.

If that ever matters, the cheap fix (separate change) is to add one
submission-evidence probe on `minds_model_state.json`'s mtime in
`mngr_codex.plugin._build_submission_evidence_probes`: the binary writes the file
the instant it applies, so the switch confirms in ~one poll interval instead of
timing out, the lock releases immediately, and switches stop serializing. Left
out of this spec by choice to keep it to the two-line-behavior change above.

## Tests (`harnesses/codex/model_test.py`)

- `read_live` maps `{model, reasoning_effort, service_tier:"priority"}` ->
  `ModelIdentity(model_id, effort, fast=True)`; `service_tier` `default`/absent
  -> `fast=False`; `model` missing -> `None`; missing file -> `None`.
- `watched_paths` returns the state-file path.
- `guess_from_launch` unchanged (existing `config.toml` test stands).
- Catalog `switch_mode == EAGER_THEN_RECONCILE`.
- Integration: writing the state file triggers `_recompute_model_choice` + a
  broadcast (mirrors the pi resolver test).

Add a changelog entry under `system/apps/system_interface/changelog/`.
