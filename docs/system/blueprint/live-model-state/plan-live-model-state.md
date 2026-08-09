# Plan: live-only model state for the model bar (claude / codex / pi)

Status: reviewed draft (three-lens review folded in, 2026-08-08). No code written yet.

Reference artifact: `statusline-payload-v2.1.207.json` (a real statusline stdin
payload captured live from this workspace's Claude Code 2.1.207).

## Behaviors being implemented

1. The model bar always shows the truth, promptly: at launch (never the shrug), and
   after `/model`, `/effort`, `/fast` -- including switches made while the agent is
   idle -- for all three harnesses. Before a session has reported anything the bar
   shows logo-only.
2. One harness-agnostic live pipeline: a single shared read -> match -> push path
   with zero harness branches. Per-harness surface is exactly four things: a
   catalog (data), a `switch()` (code), `list_offered_models()` (code; only pi
   overrides it -- its offer set is per-agent and auth-gated, feeding the search
   picker), and a native state writer (script / patch / extension).
3. UI switching reconciles through the same pipeline: the state file is the ack;
   no separate confirmation path.
4. Uniform contract: one file name, one schema, one location, all harnesses.

## Locked decisions

- Only the patched codex ("codex-in-minds") is supported; the codex launch guess is
  deleted.
- The statusline `fast_mode` field is trusted as runtime truth. No transcript
  `service_tier` double-check in the writer.
- No transition shims or dual-name reads anywhere; writers and readers flip in the
  same change ("dirty code" mode). Consequences accepted explicitly:
  - running pi agents' bars are stale until the agent is RE-CREATED (the extension
    is copied per-agent at provision; restart does not re-provision);
  - codex bars on containers still running the old patched binary are logo-only
    until the new binary is installed (codex is feature-flag gated);
  - after the system_interface restart, existing idle claude chats are logo-only
    until their statusline next fires (self-heals; at most one refresh interval
    after the pane repaints).
- The matcher key field is named `harness_reported_model_id`. Keys are
  SUFFIX-FREE raw API ids (RESOLVED: statusline `model.id` carries no `[1m]`
  suffix -- the fable capture shows `claude-fable-5`, and the old hook recorded
  raw `claude-opus-4-8`; `[1m]` is a settings alias, not a runtime id). The
  catalog has exactly one opus option, so no collision; if a plain-opus option is
  ever added, the two would collide on the reported id and the writer would need
  `context_window_size` to disambiguate (note kept here on purpose).
- Drift tolerance IS an explicit goal: the matcher keeps one prefix pass so dated
  ids (`claude-haiku-4-5-<date>`) keep matching a suffix-free key. A shrug is not
  cosmetic: with `matched=None` the fast-mode-off action no-ops
  (`ModelSettings.ts:181`), so an avoidable shrug can leave fast mode billing
  after the user declined it.
- `HarnessCatalog.default_model_id` is deleted (only consumers are the two guess
  fallbacks; the frontend declares but never reads it). The dead `QueueBehavior`
  type and `HarnessCatalog.queue_behavior` field are deleted explicitly (zero
  consumers). `ModelOption.in_picker` SURVIVES (load-bearing: `ModelBar.ts:339`
  filters the picker on it).

## Shared contract

State file, identical for all harnesses:

```
$MNGR_AGENT_STATE_DIR/minds_model_state.json
{"model": str, "effort": str | null, "fast": bool}
```

Written atomically -- tmp + rename with a FIXED tmp name in the same directory
(a fixed name self-heals orphans from cancelled writers; Claude Code cancels
in-flight statusline scripts on supersession, and mktemp-style unique names would
accumulate).

Data structures: all five types already exist in `harnesses/model.py`
(EffortChoice, ModelOption, ModelIdentity, ModelChoice, HarnessCatalog) -- this is
a MODIFICATION, not new definitions. Net changes:

- `ModelOption` gains `harness_reported_model_id: str | None = None` (None -> `id`).
- `ModelChoice` loses `source`.
- `HarnessCatalog` loses `default_model_id` and `queue_behavior`.
- `ModelIdentity` unchanged (still needed apart from ModelChoice: it is the
  `switch()` input the endpoint constructs).

Matching (one shared implementation):

```python
by_key = {opt.harness_reported_model_id or opt.id: opt for opt in catalog.options}
matched = by_key.get(identity.model_id)
if matched is None:
    matched = next((o for k, o in by_key.items() if identity.model_id.startswith(k)), None)
```

Effort/fast validity still checked against the matched option as today.

RECONCILIATION CONTRACT (review finding, all three reviewers): the pushed
`identity.model_id` becomes a raw reported id, so the frontend MUST NOT compare
identities via `baseAlias` anymore. Settle/diff moves to the backend-computed
match:

- pending overlay settles when `liveChoice.matched?.id === pending.option.id`
  (plus effort/fast equality) -- replaces `identityEquals` in
  `ModelSettings.ts:50-54,73`;
- `changedAxes` diffs the model axis against `matched.id`, not the raw identity
  (`ModelSettings.ts:92-104`, callers `ModelBar.ts:382,406-411`). Without this,
  the overlay never settles (5-minute stuck pending) and every effort/fast click
  re-sends `/model`.
- Deletions this enables: frontend `baseAlias` (`HarnessCatalog.ts:73-75`) and
  `findOption` (`HarnessCatalog.ts:78-81`, zero callers already); server-side
  `base_alias` option lookup in POST /model (`server.py:65,486-487`) becomes an
  exact `option.id == req.model_id` lookup (the picker only ever sends catalog
  ids).

## Control flow

Inbound (terminal -> UI):

```
harness event (session open / model / effort / fast change / refresh tick)
  -> native writer atomically writes minds_model_state.json
  -> per-agent PathWatcher fires (watches the parent state dir, which exists
     before the watcher starts; 1s poll backstop -- mechanics verified)
  -> shared reader: file -> ModelIdentity -> match -> ModelChoice
  -> agent store -> WebSocket push (no-op broadcast guard already dedupes the
     ~2s rewrite loop)
  -> ModelBar: matched -> chip; unmatched -> shrug; no file yet -> logo-only
```

Outbound (UI -> terminal): unchanged mechanics; reconciliation per the contract
above.

## Phase 1 -- claude writer (dwt side)

- Extend `system/scripts/claude_status_line.sh`: parse stdin JSON (`model.id`,
  `effort.level`, `fast_mode`), atomically write the unified file (fixed tmp
  name). Guards:
  - skip when `MNGR_AGENT_STATE_DIR` is unset (plain claude sessions);
  - skip when the payload `session_id` differs from
    `$MNGR_AGENT_STATE_DIR/claude_session_id` (nested-interactive-claude guard --
    without it, a nested TUI in the same pane env oscillates the file every
    refresh tick; the session-id file is already written by the SessionStart
    hook, so this is a two-line check. Worst case one skipped write at session
    start, corrected next fire).
  - Keep printing the status text; trim the per-fire `git rev-parse`.
- Add `"statusLine": {"refreshInterval": 2}` (unit: SECONDS) in
  `.claude/settings.json`. RESOLVED: 2.1.207 supports and honors it (verified in
  the installed binary); no Claude Code bump needed. Unverified minor: whether an
  already-running session picks up the new key without restart (next-message
  fallback applies regardless).
- Rollout reach: the script runs from `${MNGR_AGENT_WORK_DIR}/system/scripts/...`
  per agent checkout -- all chat agents share this checkout, so running chat
  agents pick the writer up on their next fire. Launch-task workers in worktrees
  on older branches run their branch's copy; they are headless (`claude -p`, no
  statusline) so nothing regresses there.

## Phase 2 -- demolition (both repos)

mngr side (edit the vendored tree `system/vendor/mngr`; must land on
`claude-codex-pi-mngr` too -- see ship mechanics):

- Delete `libs/mngr_claude/imbue/mngr_claude/resources/model_state_hook.py` + its
  test + the four `_WRITE_MODEL_STATE` registrations in `claude_config.py`
  (:806, :835, :862, :893) AND the two references the file leaves behind
  (review finding -- deleting without these breaks provisioning):
  - `_CLAUDE_ALWAYS_PROVISIONED_SCRIPT_NAMES` entry (`plugin.py:1393-1399`);
  - `_STANDALONE_RESOURCE_SCRIPTS` entry (`test_ratchets.py:20-27`).
- The branch's output-style / append-system-prompt work is untouched (verified
  independent).

system_interface side (dwt):

- Delete: per-harness `read_live()` / `guess_from_launch()` / `watched_paths()`;
  `merge_identities`; backend `base_alias`; `_to_catalog_model_id`; claude's
  settings/managed fallback reads + `_resolve_agent_fast_mode`; claude's
  `_write_model_state_snapshot` + its call in `switch()`
  (`claude/model.py:239-250,283-285` -- review finding: it is a second writer of
  the state file from the UI side, exactly the confirmation path this plan
  abolishes; the frontend overlay covers the gap until the next statusline fire);
  both guesses; `ModelChoice.source`; `default_model_id` (+ frontend type);
  `QueueBehavior`; the per-harness state-file name constants; the
  `_model_resolver_by_agent` cache + `get_model_resolver`
  (`agent_manager.py:346,405,456,1251-1253,1272,1288,1319-1322` -- review
  finding: recompute needs only state dir + catalog once resolvers stop doing
  live reads; the two endpoints build a resolver inline from `agent_info`, all
  three `build()`s are plain field copies).
- Add the shared reader + matcher; `_ensure_model_tracking` reduces to "watch
  `<state_dir>/minds_model_state.json` once the state dir exists".
- Frontend reconciliation change per the contract section.
- Claude catalog options gain suffix-free `harness_reported_model_id` values:
  `opus[1m] -> "claude-opus-4-8"`, `fable -> "claude-fable-5"`,
  `sonnet -> "claude-sonnet-5"`, `haiku -> "claude-haiku-4-5"` (dated/1m variants
  covered by the prefix pass). Codex/pi leave it None.
- Side effect: the launch shrug dies permanently (`<synthetic>` entered through
  the hook's transcript read; the statusline always reports the real model).

## Phase 3 -- pi writer

- `mngr_pi_coding/resources/mngr_pi_lifecycle.ts` (`MODEL_STATE_NAME` ~:162)
  writes `minds_model_state.json` with the unified schema (`"provider/model"` ->
  `model`, `thinking_level` -> `effort`, `fast: false`) and switches
  `recordModelState` from bare `writeFileSync` (:358-367) to tmp+rename (review
  finding: the current write is not atomic; the contract claims atomicity).
  Update its test.
- `harnesses/pi_coding/model.py` loses its reader; catalog + `switch()` +
  `list_offered_models()` stay.
- Reach: new/re-created pi agents only (extension copied at provision; provision
  runs only at create).

## Phase 4 -- codex writer (crosses into a third repo -- resolved by review)

Reality (was open item 3): the patch source is the external repo
`github.com/minhtrinh-imbue/codex-in-minds`. Its prebuilt per-arch binaries are
installed OVER the npm-vendored codex at Docker IMAGE BUILD only
(`system/scripts/setup_system.sh:246-275`; pinned `CODEX_PATCH_RELEASE=v0.146.0`
+ per-arch sha256s at :256-257; nothing reinstalls at boot). The codex process
does have `MNGR_AGENT_STATE_DIR` (agent env file is sourced into the pane before
launch -- verified).

Plan (AS IMPLEMENTED -- one deliberate deviation):

- DECISION taken at implementation: codex KEEPS writing at
  `$CODEX_HOME/minds_model_state.json`. `CODEX_HOME` is
  `<state_dir>/plugin/codex/home`, i.e. inside the agent state dir -- so the
  uniform contract holds with the state file's RELATIVE PATH as per-harness
  DATA on the reader side (claude/pi: state-dir root; codex:
  `plugin/codex/home`). Rationale: an env-var path in the Rust patch is
  untestable there (process-global env in parallel tests) and breaks plain
  non-mngr codex runs; a relative-path constant is data, not code.
- codex-in-minds (patch regenerated from an applied upstream tree, applies
  clean to the pristine `rust-v0.146.0` tag; committed locally on its
  `claude-codex-pi-mngr` branch, NOT pushed): writer emits the unified
  `{model, effort, fast}` schema, tier -> fast mapped in the patch
  (`"priority"` == fast), effort always present (null when none); docs updated.
- Release/binary bump DEFERRED (build.sh needs authenticated AWS EC2). Until a
  new release is pinned in `setup_system.sh`, installed binaries write the OLD
  schema at the same path: the chip still shows the right model (the `model`
  key is unchanged), effort shows none and fast shows off. Graceful, self-heals
  on the next image bake.
- In system_interface: delete `guess_from_launch` + the config.toml read; update
  docstrings (including the stale `pi_model_state.json` mention in
  `codex/model.py:18`).

## Phase 5 -- verify + ship

Tests:

- Shared reader/matcher unit tests (exact match, prefix match, unknown -> None,
  missing file -> None -> logo-only).
- Claude conformance test (doubles as the writer test): run
  `claude_status_line.sh` via subprocess with the committed real payload fixture
  on stdin, feed the produced file through the shared reader. Fixture lives in
  the system_interface suite (there is no test project for `system/scripts/`);
  source copy: `statusline-payload-v2.1.207.json` beside this plan.
- Codex conformance: a hand-captured fixture of the NEW patch's output file
  (CI cannot execute the Rust binary; the only true writer-side test would live
  in codex-in-minds itself -- scoped accordingly).
- Pi conformance: drive the extension's writer through node per the existing
  `mngr_pi_lifecycle_test.py` pattern; NOTE it `pytest.skip`s when node can't
  import TS, so assert node availability in mngr CI or accept the skip.
- Update/delete: `claude/codex/pi model_test.py`, the shared
  `harnesses/model_test.py` (merge_identities/base_alias tests die with their
  subjects), hook tests, `server_test.py`'s default_model_id consumer.
- Full suites in both projects; changelog entries in both repos (branch entry
  files already exist for the touched mngr projects; extend them).

Manual verification:

- Fresh claude agent: bar correct at launch, no shrug.
- Idle `/model` + `/effort` + `/fast` flips reflect within ~2s.
- UI-initiated switch: optimistic -> settles via matched-id (not stuck pending);
  effort/fast clicks do NOT re-send `/model`.
- Existing idle claude chats: logo-only right after the system_interface restart,
  recover on next statusline fire (expected transient, not a regression).
- Pi: re-created agent's chip live under the unified contract.
- Codex: after manual binary swap in this workspace, chip live; old-binary
  containers logo-only (expected).

Ship mechanics (review finding -- vendor coupling made explicit):

- The mngr-side changes are made in the vendored tree AND pushed to
  `claude-codex-pi-mngr` (PR #318) in the same change series; the dwt-side
  changes (reader flip, statusline writer, setup_system bump) land on
  `claude-codex-pi-dwt` (PR #390) together with that vendored state. The two
  trees must be byte-identical in the touched files at every future vendor sync,
  or the next sync reverts the writers.
- Restart `system_interface` (one deliberate moment; the app watcher does not
  auto-restart it).
- Running claude agents keep their provisioned old hook writing the dead
  `claude_model_state.json` forever -- inert by design, not a failed deploy.

## Remaining open items

1. (Minor, empirical) Confirm an opus-1M session's statusline `model.id` is
   `"claude-opus-4-8"` with no suffix (high confidence from the fable capture +
   the old hook's recorded raw ids; one-minute check when an opus session is at
   hand).
2. (Minor) Whether a running session honors a newly added `refreshInterval`
   without restart.
