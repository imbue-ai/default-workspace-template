# Antigravity CLI (`agy`) integration — file-by-file spec

Status: **design only, nothing built.** Legend: `[NEW]` create, `[EDIT]` modify.
`Q:` = implementation question I am not 100% sure about (resolve before/while
coding). Grounded against `agy --help` (v1.1.10 in this image) and
antigravity.google/docs (rules-workflows, models). Facts I could not verify are
marked `Q`, not asserted.

## Established facts (verified)

- mngr type `antigravity` (alias `agy`) is registered; `agy` v1.1.10 baked into the
  image; `imbue-mngr-antigravity` already a `system_interface` dep. Only the 5
  surfaces below are missing.
- Read the **original transcript** (per user): `<agent_state_dir>/logs/antigravity_transcript/events.jsonl`,
  the always-on SQLite→JSONL decode. NOT the lossy `events/antigravity/common_transcript/`.
- Instructions inject (DECIDED) via a per-agent global rule `~/.gemini/GEMINI.md`
  (under the per-agent `$HOME` → per-agent, no repo pollution). This is a peer of the
  repo's `AGENTS.md` (no documented precedence, deduplicated) — the precedence loss is
  **accepted**; we are NOT using the higher-precedence custom-agent (`--agent`) route.
  See Surface B.
- The 12,000-char rule cap (docs) is **not a hard truncation in practice**: a live agy
  1.1.10 read a 16,478-char `GEMINI.md` in full (canary probe returned every marker to
  offset 16,400). So the 15,773-char style body ships as-is; the plugin just logs a
  non-fatal warning above 12k. Q1 resolved.
- agy launch flags (verified via `--help`): `--model "<name>"`, `--effort low|medium|high`
  (effort is a **separate** launch axis), `--conversation <id>`, `--add-dir`,
  `--dangerously-skip-permissions`, `--mode`, `--print`/`--output-format`, `--json-schema`.
- Models (from live `agy models`, signed in): slug ids `gemini-3.6-flash-{high,medium,low}`,
  `gemini-3.5-flash-{high,medium,low}`, `gemini-3.1-pro-{high,low}`, `claude-sonnet-4-6`,
  `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`. **Effort is a separate axis**: `--model`
  + `--effort low|medium|high`. Verified live: `--model gemini-3.6-flash --effort medium` is
  valid; `--model gemini-3.6-flash-medium` (combined slug) is also valid; `--model
  gemini-3.6-flash` alone errors `requires --effort`. The Gemini models have low/med/high;
  Claude/GPT-OSS have no effort tier. Display names ("Gemini 3.6 Flash (Medium)") are labels
  shown in errors, not the identifier. `agy models` needs sign-in → catalog is hardcoded.
- No turn markers in the transcript → activity is Claude-style (lifecycle + tail),
  `special_kinds = frozenset()`.

---

## Surface A — launcher config (`.mngr/settings.toml`)

Add an `[agent_types.antigravity]` block, symmetric to `[agent_types.codex]` /
`[agent_types.opencode]` / `[agent_types.pi-coding]`. Own base type (registered by the
plugin), so **no `parent_type`**.

### The two axes, and how the peers set them

| Concern | claude | codex | opencode | pi | **antigravity (proposed)** |
|---|---|---|---|---|---|
| skip install at provision | (baked) | `check_installation=false` | `check_installation=false` | `check_installation=false` | `check_installation=false` (agy baked in) |
| unattended approvals | `--dangerously-skip-permissions` | `auto_allow_permissions=true` | `auto_allow_permissions=true` | `auto_allow_permissions=true` | `auto_allow_permissions=true` (adds agy's `--dangerously-skip-permissions`) |
| trust repo silently | `auto_dismiss_dialogs` | `auto_dismiss_dialogs=true` | — | `auto_dismiss_dialogs=true` | `auto_dismiss_dialogs=true` |
| freeze binary | version pin + `DISABLE_AUTOUPDATER` | baked + `check_installation=false` | baked | baked | `update_policy="NEVER"` (sets `AGY_CLI_DISABLE_AUTO_UPDATE=true`) |
| disable nested sandbox | n/a | `sandbox_mode="danger-full-access"` (its OS sandbox is on-by-default + broke under gVisor) | n/a | n/a | **nothing to disable** — agy's terminal sandbox is opt-in (`--sandbox` flag, which mngr never passes); optional defensive `settings_overrides.enableTerminalSandbox=false` (Q-A1) |
| model pin | `opus[1m]` | `gpt-5.6-sol` + `medium` | none (user/account) | none (user/account) | none (account-driven; Q-A2) |

### Proposed block

```toml
# the "antigravity" agent type -- Google's Antigravity CLI (agy) as a peer harness to
# claude/codex. Its own base type (registered by the vendored imbue-mngr-antigravity
# plugin via `mngr plugin add` in system/scripts/build_workspace.sh), so no parent_type.
# The agy binary is baked into the image (system/scripts/setup_system.sh,
# agy_install-1.1.10.sh), so check_installation is off. auto_allow_permissions +
# auto_dismiss_dialogs run the container agent unattended, matching agent_types.codex.
# update_policy = NEVER freezes the baked build (agy has no version pin; NEVER sets
# AGY_CLI_DISABLE_AUTO_UPDATE=true in the agent env).
#
# Model pin: pass agy's own `--model` / `--effort` launch flags via cli_args (both are
# shell-safe -- the slug and the effort word have no spaces, unlike the "Gemini 3.5 Flash
# (High)" display name). Verified against live agy: `--model <slug> --effort <low|med|high>`.
# Leave cli_args empty to use the account default (like opencode / pi-coding); the chat
# model bar (Surface D) will drive model+effort as two axes, matching this flag pair.
#
# settings_overrides is folded (last) into the per-agent settings.json over the synced
# base: the telemetry/tips/survey flags keep the chat surface quiet. NOTE: unlike codex,
# there is no sandbox to disable -- agy's terminal sandbox is opt-in via the `--sandbox`
# flag (which mngr never passes), so `run_command` already runs unrestricted in-container.
[agent_types.antigravity]
auto_dismiss_dialogs = true
auto_allow_permissions = true
check_installation = false
update_policy = "NEVER"
settings_overrides = { enableTelemetry = false, showTips = false, showFeedbackSurvey = false }
# To pin a model + effort, add agy's launch flags (both shell-safe, no spaces):
#   cli_args = ["--model", "gemini-3.6-flash", "--effort", "medium"]
```

Notes:
- **Model is a launch flag, not a settings key.** `--model <slug> --effort <tier>` is the
  verified mechanism (both forms work: `gemini-3.6-flash --effort medium`, or the combined
  `gemini-3.6-flash-medium`). agy's model persistence otherwise lives in the `config/config.json`
  `userSettings` scope, NOT `antigravity-cli/settings.json` -- so a `settings_overrides.model`
  is the wrong file; use `cli_args`. **Symmetric-with-codex design (Surface D):** add dedicated
  `model` + `effort` fields to `AntigravityAgentConfig` and have the plugin emit `--model` /
  `--effort` from them, the antigravity analog of codex's `model` / `model_reasoning_effort`.
- With `auto_allow_permissions=true`, agy's `--dangerously-skip-permissions` auto-approves
  every tool call, so `toolPermission` / a `permissions` policy in `settings_overrides`
  would be moot (skip wins) — omitted here, matching codex.
- No `[create_templates.chat]` change needed: `output_style` is harness-neutral and now
  resolves onto the antigravity config (Surface B, shipped). Verified live: `-t chat`
  resolves `output_style` + `append_system_prompt` onto `AntigravityAgentConfig` exactly
  as it does for codex.

### Open questions (Surface A)

- **Q-A1 (sandbox):** agy's terminal sandbox is opt-in (`--sandbox` flag / `enableTerminalSandbox`
  setting), and mngr passes neither, so `run_command` should already run unrestricted
  in-container — no codex-style override needed. Only add `enableTerminalSandbox=false` if a
  synced-home base turns it on. Verify a `run_command` actually executes under docker/gVisor.
- **Q-A2 (model pin): RESOLVED.** Model+effort set via `--model <slug> --effort <tier>` launch
  flags (verified live). Slugs from `agy models`. Recommend leaving unset (account default) for
  v1; pin via `cli_args` if desired, or add `model`/`effort` fields for the codex-symmetric design.
- **Q-A3 (telemetry keys):** confirm `enableTelemetry` / `showTips` / `showFeedbackSurvey`
  are honored from the per-agent `settings.json` and are scalar-assignable through
  `settings_overrides` without tripping the narrowing guard. Low risk (scalars), but
  untested; drop any that error.

---

## Surface B — instructions injection (vendored plugin)

**Mechanism (DECIDED): a per-agent global `GEMINI.md` rule.** Codex splits two
channels (`codex_config.py:358-362`, `plugin.py:645-716`): (1) `developer_instructions`
in `config.toml` — appends to the built-in system prompt, high precedence; (2)
`CODEX_HOME/AGENTS.md` — a lower peer channel. Codex puts the style in (1). agy has no
config-level (1)-equivalent; its only high-precedence route is a custom-agent body
(`--agent`), which we are **deliberately not using** — we accept the precedence loss
and use agy's channel (2): the per-agent global rule `~/.gemini/GEMINI.md`, which under
the per-agent `$HOME` is per-agent and never pollutes the repo. It is a *peer* of the
repo's `AGENTS.md` (no documented ordering, deduplicated) — accepted.

Channel (2)'s repo side needs no work: agy natively walks cwd→git-root reading
`AGENTS.md` / `.agents/rules` and our cwd symlink points at the repo, so the repo's own
`AGENTS.md` already reaches agy. We only add the per-agent global `GEMINI.md`.

### `system/vendor/mngr/libs/mngr_antigravity/imbue/mngr_antigravity/plugin.py` `[EDIT]`

`AntigravityAgentConfig` — add two fields (copy Codex `plugin.py:236-247`):
- `output_style: OutputStyleName | None = None`
- `append_system_prompt: tuple[SystemPromptText, ...] = ()`

`AntigravityAgent` — add:
- `_build_agent_rules_text(host) -> str | None`
  - `blocks = [str(p) for p in self.agent_config.append_system_prompt]`
  - if `output_style`: `blocks.append(resolve_output_style(output_style, read_output_style_files(host, get_shared_output_styles_dir(Path(self.work_dir)))))`
  - return `"\n\n".join(blocks)` or None. (same logic as Codex `_build_developer_instructions`)
- In `_provision_agy_home(...)`: after the settings write, if the text is non-empty →
  `host.write_text_file(get_antigravity_global_rules_path(agy_home), text)`. No launch-flag
  change (`GEMINI.md` is auto-discovered — no `--agent`).
  - Emit a **non-fatal** warning when `len(text) > 12000` (documented rule cap) — e.g.
    `logger.warning("Antigravity GEMINI.md is {} chars (> agy's 12000 rule cap); it may be
    truncated on some agy versions", len(text))`. Advisory only; never blocks the write.
    (Live agy 1.1.10 read a 16,478-char file in full, so this is a guard against future
    versions, not a known failure.)

### `.../mngr_antigravity/antigravity_config.py` `[EDIT]`
- `get_antigravity_global_rules_path(home: Path) -> Path` → `home/".gemini"/"GEMINI.md"`
  (or the `rules/` variant per Q1b).
- (only if Surface D option 2) `ACTIVE_MODEL_FILENAME: str = "active_model"` +
  `get_active_model_file_path(state_dir) -> Path`.

### `.../mngr_antigravity/resources/statusline.sh` `[EDIT]` (only if Surface D option 2)
- Parse `.model` from the stdin payload JSON (jq, same as `agent_state`/`conversation_id`)
  and write it to `$MNGR_AGENT_STATE_DIR/active_model`. `Q7:` needed only if
  settings.json is insufficient for `read_live()`.

### `README.md` `[EDIT]`, `changelog/<branch>.md` `[NEW]`
### Tests `[EDIT]`: `plugin_test.py` (fields + rules file written), `antigravity_config_test.py` (path helper), `statusline_test.py` (model capture, if done).

---

## Surface C — UI harness `harnesses/antigravity/`

### `harnesses/harness_type.py` `[EDIT]`
- `ANTIGRAVITY = "antigravity"` on `HarnessType`.

### `harnesses/antigravity/__init__.py` `[NEW]` — blank.

### `harnesses/antigravity/session_parser.py` `[NEW]`
Maps one raw JSONL record → 0+ UI events. Raw shape:
`{step_index, source, type, status, created_at, _mngr_conv_id, content?, thinking?, tool_calls:[{name,args}]?}`.
- `SOURCE = "antigravity/transcript"` (label only; nothing branches on it).
- `_event_id(conv, step, kind) -> str` → `f"agy-{conv}-{step}-{kind}"` (stable, position-independent; agy has no per-item id).
- `_labelled_tool_call(call_id, name, args_json) -> dict` → `{tool_call_id, tool_name, input_preview (truncated unless keeps_full_tool_input), header_label, caption_label}`.
- `_assistant_event(ts, id, *, text, thinking, tool_calls) -> dict` → `type=assistant_message`, `model="unknown"`, plus `message_uuid`, `stop_reason=None`, `usage=None`, `is_auth_error=False`.
- `_user_event(ts, id, content) -> dict`.
- `_tool_result_event(ts, id, call_id, name, output, is_error) -> dict`.
- `parse_record(record, tool_name_by_call_id: dict) -> list[dict]`:
  - `USER_EXPLICIT`/`USER_INPUT` → `[user_event]`.
  - `MODEL`/`PLANNER_RESPONSE` → one `assistant_event` (text from `content`; **may carry text AND tool_calls in one record**, unlike Codex); each `tool_calls[i]` → a labelled call; register `call_id → name`.
  - `MODEL`/`CODE_ACTION` → `[tool_result_event]`, `is_error = status != "DONE"`, paired to the last unpaired call in the same `_mngr_conv_id`.
  - `ERROR_MESSAGE` → `Q4` (surface as error event vs fold into assistant text).
  - `SYSTEM`/`SYSTEM_MESSAGE`/`CONVERSATION_HISTORY`/`USER_IMPLICIT` → `[]`.
  - `Q3:` `thinking` field — render as part of assistant text, a dedicated reasoning
    part, or drop? Depends on frontend support.
  - `Q5:` tool-call id — agy carries none; `agy-<conv>-<step>-tc<idx>` is our synth.
    Confirm `content`/`args` field names against a real transcript (from decoder, not
    guessed).

### `harnesses/antigravity/watcher.py` `[NEW]`
`AntigravitySessionWatcher(AgentSessionWatcher)` — simpler than Codex (single stable
append-only file, no rotation/marker).
- `resolve_raw_transcript_path(agent_state_dir) -> Path` → `.../logs/antigravity_transcript/events.jsonl`.
- `build(agent_info, on_events)`, `start()`/`stop()` (recursive `PathWatcher` on `logs/antigravity_transcript/`), `_emit_unsent()`, `_consume_new_lines()` (byte-offset incremental, bytes-partial carry, split on `\n`), `_adapt_line()`, `_ingest_event()` (append/supersede/dedup by event_id), plus the read API (`get_all_events`, `get_tail_events`, `get_backfill_events`, `get_forward_events`, `get_events_at_offset`, `get_event_offset`, `get_total_event_count`), `get_subagent_metadata → None`, `is_main_session_event → True`.
- `Q2:` does the decoder ever shrink/rewrite the file across `mngr stop`/`start` or
  when interleaving multiple conversation ids (root + subagents in one stream)? If
  yes, add Codex's "shrink → re-read from 0, dedup covers it" guard. Confirm against
  `decode_agy_transcript.py` offset handling + a live resume.

### `harnesses/antigravity/tool_labels.py` `[NEW]`
- `tool_labels(tool_name, args_json) -> (header_label, caption_label)`.
- `keeps_full_tool_input(tool_name, args_json) -> bool` (patch/tk bodies whole, if applicable).
- `_LABELS_BY_TOOL: dict[str, (noun, verb)]` + per-tool target extraction (path/cmd/query).
- `Q5:` agy's real tool surface + `args` shape is unknown without a live transcript
  (binary hints: `run_command`, browser tools, file read/write/edit, `view_image`,
  web/search, MCP). Ship a generic `Tool: <name>` label first; refine from real data.
  Do NOT invent the map.

### `harnesses/antigravity/activity_state.py` `[NEW]`
- `derive(*, is_agent_running, has_pending_tool_use, tail_event_at, process_started_at) -> ActivityState`
  — Claude-style: stale tail → IDLE; not running → IDLE; pending tool → TOOL_RUNNING;
  running → THINKING. (agy's `active` marker makes `is_agent_running` reliable, unlike
  Claude, so trust it.)

### `harnesses/antigravity/activity.py` `[NEW]`
- `AntigravityActivityTracker(HarnessActivityTracker)`: `marker_filename` (`Q6:` confirm
  `antigravity_process_started` is what the plugin/base writes on start/resume), `observe()`,
  `derive()` delegating to activity_state.

### `harnesses/antigravity/model.py` `[NEW]` — the model bar. Detailed in **Surface D** below.

### `harnesses/antigravity/icon.svg` `[NEW]` — Antigravity/Gemini logo.

### `harnesses/registry.py` `[EDIT]`
- Import the 4 classes + catalog; add `HarnessType.ANTIGRAVITY: HarnessSpec(... special_kinds=frozenset())`.

---

## Surface D — model bar (READ_ONLY, from a persisted launch file)

**Design (decided): read model/effort from a PERSISTED file the plugin writes at launch —
never from live events (statusline) or the transcript.** Two facts make this the right call:
the bar is READ_ONLY (agy has no scriptable `/model`), and the model is PINNED by the
launcher (`model`/`effort`, §Surface A). So the model is a static per-agent fact, and the
robust source is the pin itself, persisted to disk — survives restarts, depends on no live
event firing. (This drops the earlier statusline-writes-the-model idea entirely.)

**The bridge (`--model`/`--effort` → the resolver):** the flags are ephemeral; nothing reads
them back. So wherever the plugin emits `--model`/`--effort` from the `model`/`effort` config
fields, it ALSO writes those same values to a persisted file. The resolver reads that file.
Same source as the flags, re-written on every launch → the file always matches exactly what
agy was launched with, across restarts and config changes. We persist OUR slug + effort (the
catalog ids), so there is **no display-name parsing and no display→slug map** — the earlier
statusline approach needed both.

Prereq: registering the harness needs the full `HarnessSpec` (watcher + tracker, Surface C)
so `HarnessType.ANTIGRAVITY` is registerable. The model bar rides on that.

### `mngr_antigravity/plugin.py` `[EDIT]` — write the persisted model file at launch
- In `assemble_command`, at the point where `--model`/`--effort` are appended from
  `agent_config.model` / `agent_config.effort`, also persist them. Recommended: a shell
  prelude in the launch command that writes the JSON every start (so file == flags on every
  restart), e.g. `mkdir -p <state> && printf '%s' '{"model":"<m>","effort":"<e>"}' > <model_file>`.
  (Alternative: write it once in `provision` — simpler, but re-provision needed to change.)
  When `model` is unset (no pin), write nothing.

### `mngr_antigravity/antigravity_config.py` `[EDIT]`
- `MODEL_FILENAME: str = "antigravity_model.json"` + `get_antigravity_model_path(state_dir)`.
- Add it to `_antigravity_preserved_items()` (survives destroy/adopt).
- Test: `assemble_command` writes `{model, effort}` when pinned; nothing when unset.

### `harnesses/antigravity/model.py` `[NEW]`
- `ANTIGRAVITY_CATALOG: HarnessCatalog` — hand-written (codex-style), `PickerMode.LIST`,
  `switch_mode=SwitchMode.READ_ONLY`, `default_model_id="gemini-3.6-flash"`, `icon_svg`. Options
  (id = the slug the launcher pins / the file holds; label = display; efforts per model):
  | id | label | efforts |
  |---|---|---|
  | `gemini-3.6-flash` | Gemini 3.6 Flash | low, medium, high |
  | `gemini-3.5-flash` | Gemini 3.5 Flash | low, medium, high |
  | `gemini-3.1-pro` | Gemini 3.1 Pro | low, high |
  | `claude-sonnet-4-6` | Claude Sonnet 4.6 | (none) |
  | `claude-opus-4-6-thinking` | Claude Opus 4.6 | (none) |
  | `gpt-oss-120b-medium` | GPT-OSS 120B | (none) |
  `supports_fast=False` everywhere. The `id` is exactly the `model` value the launcher writes
  to the file, so `read_live` matches it directly — no translation.
- `AntigravityModelResolver(HarnessModelResolver)`:
  - `build(agent_info)` → store `agent_state_dir`.
  - `guess_from_launch()` → read the persisted file → `ModelIdentity(model_id=<model>, effort=<effort or None>, fast=False)`; None when the file is absent/empty (unpinned agent → logo-only).
  - `read_live()` → read the same file (so a restart with a changed pin reflects); None if absent. **Persisted, not live.**
  - `watched_paths()` → `(state_dir / MODEL_FILENAME,)` (a restart rewrite re-fires the recompute).
  - `list_offered_models()` → None (small static catalog).
  - `switch(identity, axes, send)` → **READ_ONLY**: sends nothing, returns
    `SwitchResult(ok=False, detail="Antigravity model switching is not available from the bar")`
    (endpoint → 409). To change the model, re-pin `model`/`effort` (Surface A) and recreate.
- No statusline dependency, no display-name parsing: the file holds the catalog id + effort.

### `harnesses/registry.py` `[EDIT]`
- Import `ANTIGRAVITY_CATALOG` + `AntigravityModelResolver` (+ the Surface C watcher/tracker);
  add the `HarnessType.ANTIGRAVITY` `HarnessSpec` with `catalog_factory=lambda: ANTIGRAVITY_CATALOG`.

### `harnesses/harness_type.py` `[EDIT]` — `ANTIGRAVITY = "antigravity"` (shared with Surface C).

### Open questions (Surface D)
- **O7 (switch): RESOLVED — READ_ONLY.** agy has no scriptable `/model <slug>` (its `/model` is
  an interactive picker), so the bar is display-only; `switch` returns 409.
- **Source: RESOLVED — persisted launch file.** model/effort come from the pin the plugin
  writes to `antigravity_model.json` at launch (same values as `--model`/`--effort`), read by
  the resolver. No statusline, no transcript, no display→slug parsing. Restart-robust.
- (No Q-D2: persisting our own slug+effort removes the display-name parsing the statusline
  approach would have needed.)

---

## Surface E — new-agent launcher (mirror Codex)

### `models.py` `[EDIT]`
- `CreateAntigravityRequest(FrozenModel)` with `name: str` (copy `CreateCodexRequest`).

### `server.py` `[EDIT]`
- `_is_antigravity_enabled() -> bool` (`FEATURE_FLAG_ENABLE_ANTIGRAVITY`).
- `_inject_enable_antigravity_meta_tag(html)` + call it beside the codex one.
- `_create_antigravity_agent()` → `agent_manager.create_chat_agent(req.name, HarnessType.ANTIGRAVITY)`.
- Register `/api/agents/create-antigravity`.
- In `_get_harnesses_endpoint`: include the antigravity catalog only when the flag is on.

### Frontend
- `frontend/src/base-path.ts` `[EDIT]`: `isAntigravityEnabled()` (reads `system-interface-enable-antigravity` meta).
- `frontend/src/views/CreateAgentModal.ts` `[EDIT]`: add `"antigravity"` mode → endpoint `/api/agents/create-antigravity`, title "Create Antigravity Agent".
- `frontend/src/views/DockviewWorkspace.ts` `[EDIT]`: `showNewAntigravityModal` state + "New Antigravity Agent" button (gated on `isAntigravityEnabled()`) + modal wiring.
- Rebuild `frontend/dist`.

### Tests `[NEW/EDIT]`
- `harnesses/antigravity/{session_parser,watcher,tool_labels,model,activity_state}_test.py`.
- `harnesses/activity_test.py` `[EDIT]`: add antigravity rows (`test_every_harness_has_a_spec` auto-covers the spec).
- `server_test.py` `[EDIT]`: create-antigravity endpoint + catalog gate.
- Frontend `*.test.ts` mirrors of the codex cases.
- `changelog/<branch>.md` `[NEW]`.

---

## Open questions (ranked)

- **Q1 (blocks B): RESOLVED.** Live agy 1.1.10 read a 16,478-char `GEMINI.md` in full
  (canary probe returned every marker). Ship the full body; plugin logs a non-fatal
  warning above 12k. No split/condense needed.
- **Q(O7) (blocks D.switch):** `/model` one-shot vs pane-wedging picker; effort switch path.
  `tmux send-keys` against a live agy.
- **Q2 (blocks C.watcher):** raw transcript shrink/rewrite on resume + multi-conversation
  interleaving in one file.
- **Q5 (C.labels, non-blocking):** agy tool surface + `args` shape — from a real transcript.
- **Q3/Q4 (C.parser):** render `thinking` / `ERROR_MESSAGE` how? Check frontend event support.
- **Q6 (C.activity):** exact `*_process_started` marker antigravity writes.
- **Q7 (D.resolver):** is settings.json enough for `read_live`, or is the statusline
  `model` capture needed?
- **Q8 (A):** `-t chat` resolves `output_style` onto the config field cleanly?
- **Q9 (A):** pin a default model or leave unset?

**Empirical gates still needing a signed-in agy + a real transcript: Q(O7), Q2, Q5, Q3.**
(Q1 done.) Do these in a live agy session before writing the corresponding code.

## Vendored-code note
Surface B (and D option 2) edit `system/vendor/mngr/libs/mngr_antigravity/` — the
vendored mngr subtree, with its own changelog/test conventions and an upstream. Decide:
land in the subtree, or in a standalone mngr clone under `.external_worktrees/` for
upstreaming. Everything else is workspace-local.
