# opencode + pi-coding harnesses — file-by-file integration spec

Status: **launchers + pre-turn-1 model/effort emission DONE (committed 381fb5ec); the
rest is design only.** Legend: `[DONE]` shipped, `[NEW]` create, `[EDIT]` modify,
`Q:` = a decision to resolve while coding. Grounded against the installed binaries
(opencode 1.18.14, pi 0.83.0), their docs, and the live codex/claude harnesses this
mirrors. The model-bar contract is `docs/model_bar_plan.md`; codex is the reference
implementation for every backend piece.

Two agent types, already registered by their vendored plugins and baked into the image:
`opencode` and `pi-coding` (short alias `pi`). The `HarnessType` string MUST equal these.

---

## Established facts (verified)

**Shared / system-interface side.** Adding a harness is one `HarnessType` enum member
(`harnesses/harness_type.py`) + one `HarnessSpec` in `harnesses/registry.py`
(`watcher_class`, `tracker_class`, `resolver_class`, `catalog`, `special_kinds`) + a
`harnesses/<h>/` package (`watcher.py`, `session_parser.py`, `activity.py`,
`activity_state.py`, `model.py`, `tool_labels.py`, `icon.svg`). The frontend needs **zero**
per-harness code: `GET /api/harnesses` serves each catalog + `icon_svg`, `model_choice`
rides the agents WebSocket, `POST /api/agents/<id>/model` routes to `resolver.switch()`.
`parse_harness` defaults unknown types to CLAUDE, so until the enum member exists an
opencode/pi agent renders as a broken claude tab.

**Both plugins now emit a model-state file (this is the pre-turn-1 collection, DONE):**
- pi → `<agent_state_dir>/pi_model_state.json` = `{provider, model, thinking_level}`,
  written on `session_start` (fires before the first prompt → true pre-turn-1 value) and
  on `model_select` / `thinking_level_select`.
- opencode → `<agent_state_dir>/opencode_model_state.json` = `{provider, model, variant}`,
  written on each assistant `message.updated` (`variant` "default" = base profile). opencode
  cannot report before the first assistant message → the resolver falls back to `opencode.json`.

**pi specifics.** No shell-hook surface; the lone lifecycle surface is the TS extension
loaded with `pi -e <state>/commands/mngr_pi_lifecycle.ts`. Markers under `<state>`:
`active` (RUNNING), `pi_session_started` (readiness), `pi_session_file` (→ live native
session JSONL, the rotation pointer). Native transcript: JSONL under
`<state>/plugin/pi_coding/sessions/--<cwd>--/<ts>_<uuid>.jsonl`, tree-structured, with
`message`, `model_change` (`{provider, modelId}`), and `thinking_level_change`
(`{thinkingLevel}`) lines. mngr transcript layers: raw
`<state>/logs/pi-coding_transcript/events.jsonl` (on `message_end`), common
`<state>/events/pi-coding/common_transcript/events.jsonl`. CLI: `--model provider/id[:thinking]`,
`--provider`, `--thinking off|minimal|low|medium|high|xhigh|max`, `--append-system-prompt <text|@file>`
(repeatable), `--system-prompt`. Extension: events `session_start(reason)`, `model_select(event.model{provider,id}, source)`,
`thinking_level_select(event.level)`, `before_agent_start` (can rewrite `event.systemPrompt` /
`systemPromptOptions.appendSystemPrompt`); reads `ctx.model`, `ctx.thinkingLevel`,
`pi.getThinkingLevel()`; writes `pi.setModel(model)` (via `ctx.modelRegistry.find(provider,id)`),
`pi.setThinkingLevel(level)`. `PiCodingAgentConfig` has NO `output_style` / `append_system_prompt` /
`model` fields. `**No `*_process_started` marker exists.**`

**opencode specifics.** Client-server: `opencode serve` (headless, the plugin runs here,
`MNGR_OPENCODE_ROLE=server`) + `opencode attach` TUI. Markers under `<state>`: `active`,
`permissions_waiting`, `opencode_root_session`, `opencode_server_port`, `opencode_ready`.
Transcript: raw `<state>/logs/opencode_transcript/events.jsonl` (per `message.updated` /
`message.part.updated`, the most-live signal), common
`<state>/events/opencode/common_transcript/events.jsonl` (full rebuild on `session.idle`).
`message.info` carries `id, role, sessionID, time.created, providerID, modelID, variant, finish`.
Events: `server.connected`, `session.created/updated/status/idle`, `message.updated`,
`message.part.updated`, `permission.asked/replied`. Server HTTP: `GET /config`,
`GET /config/providers` (→ `{providers, default:{provider:model}}`), `POST /session/:id/message`
accepts `model:{providerID, modelID}` and `variant`. Config: `opencode.json` at
`<state>/plugin/opencode/config/opencode.json` — `model` ("provider/model"), `small_model`,
`provider.<p>.models.<m>.variants.<name>` = `{reasoningEffort}` (openai) / `{thinking:{...}}`
(anthropic), `instructions` (list of file paths/globs/URLs, merged with AGENTS.md into
context), `permission`. **Effort axis = the variant** (cycled with ctrl+t / `variant.cycle`;
default = first variant). `OpenCodeAgentConfig` has NO `output_style` / `append_system_prompt`;
`config_overrides` carries `model`. **No `*_process_started` marker exists.**

---

## A. settings.toml launchers — [DONE]

`.mngr/settings.toml` has `[agent_types.opencode]` (`auto_allow_permissions`,
`check_installation=false`) and `[agent_types.pi-coding]` (`auto_allow_permissions`,
`auto_dismiss_dialogs`, `check_installation=false`). No model pinned (many-auth). Verified
via `mngr config get`. Bare `mngr create --type opencode` / `--type pi` works today.

**Blocker for chat/automation/worker roles:** the `chat` role template sets
`output_style` and the `automation`/`caretaker`/`worker` templates set
`append_system_prompt`. Both are **codex/claude-only config fields**; `apply_create_template`
rejects a template key the resolved agent-type config does not declare. So
`mngr create --type opencode -t chat` fails until section B lands. This is the gate before a
usable chat tab on either harness.

---

## B. System-prompt / output-style injection — [NEW] config fields + wiring

Goal (mirrors codex, which feeds `output_style` + `append_system_prompt` into config.toml
`developer_instructions`): add both fields to each plugin's config and route them into the
harness's system prompt. Resolve the output-style body exactly as codex does —
`imbue.mngr.agents.output_styles.read_output_style_files` + `resolve_output_style` against
`get_shared_output_styles_dir(work_dir)` (`.agents/output-styles/`).

### pi — [EDIT] `libs/mngr_pi_coding/imbue/mngr_pi_coding/plugin.py`
- `PiCodingAgentConfig`: add `output_style: OutputStyleName | None = None` and
  `append_system_prompt: tuple[str, ...] = ()` (match codex's shapes).
- `assemble_command`: resolve the style body + join the append blocks, and pass each as a
  separate `--append-system-prompt <text>` arg (pi supports repeats; append text is verbatim,
  no file needed). This is the least-code path and the exact analogue of codex's
  developer_instructions. Effort/model flags stay as they are (`--model`/`--thinking` from
  cli_args when the user pins one).
- Q1: `--append-system-prompt` appends to pi's built-in coding prompt (like codex). An
  output style that means to *replace* the built-in prompt cannot be expressed this way —
  accept the append semantics (same limitation codex documents), or use `--system-prompt`
  to replace. Recommend append (parity with codex).

### pi system-reminders (optional, richer) — [NEW] `resources/` reminder + provisioning
`pi-system-reminders` injects dynamic `<system-reminder name="...">...</system-reminder>`
tags via event-driven `.ts` files in `<state>/plugin/pi_coding/reminders/` (each exports a
default fn returning `{on, when({branch,ctx,event}), message, cooldown, once}`). Use this
for Claude-Code-style nudges (e.g. the tk step-tracking reminder, uncommitted-changes
reminder) rather than one-shot system-prompt text. Provision a mngr-owned reminder file
alongside the lifecycle extension. Q2: keep reminders minimal/off for v1 (the append-system-prompt
covers the output style); add specific reminders only if drift shows.

### opencode — [EDIT] `libs/mngr_opencode/imbue/mngr_opencode/plugin.py` + `opencode_config.py`
opencode has no inline system-prompt config — only `instructions` (a list of files merged
into context). So:
- `OpenCodeAgentConfig`: add `output_style` + `append_system_prompt` (same shapes).
- Provisioning (`build_opencode_config` / `_provision*`): resolve the style body + append
  blocks, write them to a mngr-owned file, e.g. `<config_dir>/mngr_instructions.md`, and
  prepend that path to the `instructions` list in the generated `opencode.json` (before the
  `config_overrides` merge, so a user override still wins). This is the file-based analogue
  of codex's developer_instructions.
- Q3: `instructions` files are *merged with* AGENTS.md, so an output style that suppresses
  the built-in prompt can't be expressed (append-only, same as codex/pi). Accept.

---

## C. Backend harness wireup — [NEW] `harnesses/opencode/` and `harnesses/pi/`

Mirror `harnesses/codex/` exactly. Per harness:

### `[EDIT] harnesses/harness_type.py`
Add `OPENCODE = "opencode"` and `PI = "pi-coding"` to `HarnessType`.

### `[EDIT] harnesses/registry.py`
Add two `HarnessSpec` entries (imports for each `watcher/tracker/resolver/catalog`).
`special_kinds`: see activity below.

### `[NEW] harnesses/<h>/watcher.py` + `session_parser.py`
Tail the transcript, parse to the common event schema (`user_message`, `assistant_message`,
`tool_result`, `special`), dedup by a **stable `event_id`**, fan out via `on_events`. Reuse
`path_watch.PathWatcher` and the codex watcher's byte-safe incremental read.
- **pi:** tail the **native session JSONL** via the `pi_session_file` marker (rotation
  pointer, exactly like codex's `codex_transcript_path`). Parse pi's tree JSONL `message`
  lines → events; `event_id` = pi entry id (`message.id`). Low-latency (pi writes it live).
  `special_kinds`: pi has no turn markers in the JSONL → likely `frozenset()`, or synthesize
  turn boundaries from `agent_start`/`agent_end` if we route those through a marker (Q4).
- **opencode:** tail the **raw** `logs/opencode_transcript/events.jsonl` (per part-update,
  the live signal; the common transcript only rebuilds on idle). Parse `{type, properties}`
  message/part events → events; `event_id` from opencode's `prt_`/message id (per the
  events.py spine note). `special_kinds`: `frozenset()` (no turn markers; activity from
  `session.status`, see below).
- Q5: opencode's raw stream is per-`message.part.updated` (streaming deltas) — the parser
  must fold parts by `messageID` like the .ts `accumulateMessageEvent` does, or reuse the
  already-common `events/opencode/common_transcript` (simpler, but turn-latency). Recommend
  parsing raw for the live "thinking→streaming" feel.

### `[NEW] harnesses/<h>/activity.py` + `activity_state.py`
`HarnessActivityTracker` subclass + pure `derive()`.
- `marker_filename`: the `*_process_started` staleness bound. **Neither plugin writes one
  today** → [EDIT] each plugin's launch to `touch <state>/{pi,opencode}_process_started` on
  every start/resume (codex does this in `assemble_command`; opencode in `opencode_launch.sh`;
  pi in `assemble_command` before exec). Without it the restart-staleness guard can't fire.
- Derivation: opencode has a real busy/idle signal (`session.status` busy→`active` marker,
  idle clears) — model it like codex's turn latch, OR use the `active` marker + tail
  staleness (claude-style). pi has `agent_start`/`agent_end` (active marker) — same choice.
  Q6: cleanest is to have each plugin emit `turn_started`/`turn_completed` `special` events
  (codex-style latch) — but that's more plugin work; a claude-style lifecycle+tail heuristic
  needs no new signal. Recommend claude-style heuristic for v1.

### `[NEW] harnesses/<h>/tool_labels.py` + `icon.svg`
Tool-name → human label map (both surface bash/edit/read/etc.). Monochrome `currentColor`
logo per harness.

---

## D. Model chooser (guess) + changer (switch) + catalog — [NEW] `harnesses/<h>/model.py`

`HarnessModelResolver` subclass + `HarnessCatalog` (`options`, `default_model_id`,
`switch_mode`, `icon_svg`), registered in `registry.py`.

### `guess_from_launch()` (pre-turn-1)
- **pi:** read `pi_model_state.json` if present (written at `session_start`); else read pi
  `settings.json` `defaultModel` + launch `--thinking`; else catalog default. Returns
  `ModelIdentity(model_id="provider/model", effort=<thinking>, fast=False)`.
- **opencode:** read `opencode_model_state.json` if present; else read `opencode.json`
  `model` (+ the default/first variant of that model); else `GET /config/providers` default;
  else catalog default.

### `read_live()` + `watched_paths()`
Both read their `*_model_state.json` (the plugin keeps it live) → `ModelIdentity`.
`watched_paths()` = that file. `switch_mode = ON_CHANGE` (chip follows disk, no optimistic move).

### `switch()` (model changer)
- **pi:** the inbox path delivers *user messages*, not TUI commands, so it can't send
  `/model`. [EDIT] the lifecycle extension to watch a control file
  `<state>/pi_control.jsonl` (same pattern as the inbox) and apply
  `pi.setModel(ctx.modelRegistry.find(provider,id))` / `pi.setThinkingLevel(level)`.
  `switch()` appends to that file. Effort axis = thinking level.
- **opencode:** [EDIT] `send_message` (or a new control path) to include
  `model:{providerID,modelID}` and `variant` in the prompt POST
  (`POST /session/:id/message` supports both), or add a session model-set call. Until
  verified end-to-end, ship `SwitchMode.READ_ONLY` and flip to ON_CHANGE once confirmed.
- Q7: confirm opencode's per-message `model`/`variant` actually re-pins the session model
  server-side (vs one-shot for that message) before enabling switching.

### Catalog + the effort-enum wrinkle
- **pi:** models = pi's providers (many-auth; a bundled default list + 🤷 fallback for
  off-catalog, like codex). Effort = thinking levels. **`EffortLevel` (harnesses/model.py)
  is missing `off` and `minimal`** → [EDIT] add `OFF="off"`, `MINIMAL="minimal"` to the
  enum for pi.
- **opencode:** effort axis = **variant**, whose names are **user-defined** in `opencode.json`
  (`high`/`low`/… but arbitrary), so they do NOT map onto the fixed `EffortLevel` enum. Q8:
  either (a) allow a harness to declare a free-form effort-label set (extend `EffortChoice`
  to carry an arbitrary label, not just an `EffortLevel`), or (b) read the variant names from
  the agent's `opencode.json` at catalog-build time (dynamic per agent, unlike today's static
  compile-time catalog). Recommend (a): a small `EffortChoice.label: str` escape hatch so
  opencode variants render as-is; this is the "their shape differs, and that's fine" seam.

### Exposure — [EDIT] feature flags
Mirror `FEATURE_FLAG_ENABLE_CODEX`: add `FEATURE_FLAG_ENABLE_OPENCODE` / `_PI` (server.py
meta tag + `/api/harnesses` gate + `flip_feature_flags.sh` + `CreateAgentModal.ts` launcher
entries), so each dark-launches independently.

---

## Build order

1. **B (system prompt)** — unblocks chat-role creation; smallest, highest-value. Ship pi
   (`--append-system-prompt`) and opencode (`instructions` file) with tests.
2. **`*_process_started` markers** — one-line touch in each launch path; needed by C.
3. **C + D for one harness end-to-end (pi first — richest, native live JSONL)**: enum +
   registry + watcher/parser + activity + resolver/catalog + icon, flag-gated. Then opencode.
4. **Model changer** — pi control-file + extension apply; opencode prompt-model after Q7.
5. **Effort-enum escape hatch (Q8)** with opencode's variant catalog.

## Open questions (carry into build)
Q1 append-vs-replace system prompt (pi). Q2 pi reminders scope. Q3 opencode instructions
append-only. Q4 pi turn markers. Q5 opencode raw-vs-common transcript source. Q6 activity
model (latch vs heuristic). Q7 opencode server model re-pin semantics. Q8 free-form effort
labels for opencode variants.
