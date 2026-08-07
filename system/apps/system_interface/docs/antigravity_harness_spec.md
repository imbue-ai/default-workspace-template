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
- Models (docs): Gemini 3.6 Flash (Low/Med/High), Gemini 3.5 Flash (Low/Med/High),
  Gemini 3.1 Pro (Low/High), Claude Sonnet 4.6 (thinking), Claude Opus 4.6 (thinking),
  GPT-OSS-120b (Medium). Selection is "sticky between user messages within a
  conversation." `agy models` needs sign-in → catalog is hardcoded.
- No turn markers in the transcript → activity is Claude-style (lifecycle + tail),
  `special_kinds = frozenset()`.

---

## Surface A — launcher config

### `.mngr/settings.toml` `[EDIT]`
Add block (mirrors `[agent_types.codex]`, own base type, no `parent_type`):
```toml
[agent_types.antigravity]
auto_dismiss_dialogs = true
auto_allow_permissions = true
check_installation = false
update_policy = "NEVER"
# model = "Gemini 3.5 Flash (High)"   # Q9: pin default or leave unset
```
No `[create_templates.chat]` change — its `output_style` is harness-neutral and
starts applying to antigravity once Surface B lands.
`Q8:` confirm a `-t chat` create resolves `output_style` onto the antigravity config
field without the `-S`-layering caveat that Claude's `fastMode` hit (§ codex has no
such issue, so likely fine).

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

### `harnesses/antigravity/model.py` `[NEW]`
- `ANTIGRAVITY_CATALOG: HarnessCatalog` — options with **per-model** effort sets
  (Gemini Flash: low/med/high; Gemini Pro: low/high; Claude/GPT-OSS: their single
  fixed qualifier), `supports_fast=False`, `default_model_id` (Q9), `switch_mode` (Q2/O7),
  `icon_svg`.
- `_display_name(model_id, effort) -> str` and `_parse_display_name("<Name> (<Effort>)") -> ModelIdentity` (id + effort, effort parsed out of the parenthetical).
- `AntigravityModelResolver(HarnessModelResolver)`:
  - `guess_from_launch()` → read `settings.json` `model` key (via `get_antigravity_settings_path`), parse; default from catalog.
  - `read_live()` → active_model file (option 2) or re-read settings.json (option 1). `Q7`.
  - `watched_paths()` → that file.
  - `switch(identity, axes, send)` → `Q(O7)`: if `/model <name>` is one-shot,
    `send("/model <display name>")` (+ effort); `SwitchMode.ON_CHANGE`. If it opens a
    picker (wedges the pane, like pre-patch Codex), fall back to READ_ONLY or a
    settings-rewrite-then-restart changer. **Must verify before writing `switch()`.**

### `harnesses/antigravity/icon.svg` `[NEW]` — Antigravity/Gemini logo.

### `harnesses/registry.py` `[EDIT]`
- Import the 4 classes + catalog; add `HarnessType.ANTIGRAVITY: HarnessSpec(... special_kinds=frozenset())`.

---

## Surface D — covered inline in `harnesses/antigravity/model.py` + (option 2) the plugin statusline edit above.

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
