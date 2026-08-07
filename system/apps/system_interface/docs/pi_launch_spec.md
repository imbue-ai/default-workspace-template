# Launching pi as a chat harness — file-by-file spec

Scope: everything to make `pi-coding` a real chat tab, pi first. Legend: `[DONE]`
shipped, `[NEW]` create, `[EDIT]` modify, `Q:` = confirm while coding. Grounded on
the installed pi 0.83 (`/usr/local/lib/node_modules/@earendil-works/pi-coding-agent`),
its docs, and the codex harness as the reference for every backend piece.

Key facts (verified this session):
- Agent type `pi-coding` (alias `pi`), plugin already registered + baked in the image.
- `PI_CODING_AGENT_DIR` overrides `~/.pi/agent` (env-vars doc); mngr points it at
  `<agent_state_dir>/plugin/pi_coding`. pi reads its global `settings.json`, `AGENTS.md`,
  `SYSTEM.md`, `APPEND_SYSTEM.md` from there.
- `APPEND_SYSTEM.md` is a real convention (usage.md line 115): pi **appends** it to the
  default system prompt; `SYSTEM.md` would **replace**. `sync_home_settings` copies only
  `settings.json` + resource dirs, NOT `APPEND_SYSTEM.md`, so provisioning can own it.
- The lifecycle extension is provisioned to `<state>/commands/mngr_pi_lifecycle.ts` and
  loaded with `pi -e`.

---

## A. Launcher (settings.toml) — [DONE, one gap]

`[agent_types.pi-coding]` exists (`auto_allow_permissions`, `auto_dismiss_dialogs`,
`check_installation=false`, no model pinned). `mngr create --type pi` works.

**Gap:** the `chat` role template sets `output_style`, which `PiCodingAgentConfig` does
not declare, so `apply_create_template` rejects `mngr create --type pi -t chat`. Section C
closes this. (Same for `append_system_prompt` from the automation/worker roles.)

---

## B. Pre-turn-1 guess (the launcher's model, populated) — [DONE]

The launcher pins no model, so the model is whatever pi resolves from its `settings.json`
`defaultModel` / launch args. The lifecycle extension writes it to disk at startup:
`session_start` fires at TUI startup **before the first prompt**, reads `ctx.model` +
`ctx.thinkingLevel`, and writes `<state>/pi_model_state.json` = `{provider, model, thinking_level}`
(and rewrites on `model_select` / `thinking_level_select`). So pi's pre-turn-1 model+effort
is on disk immediately — **no probe needed** (unlike opencode). This is the guess source
for the resolver in section E; it also serves as the live source.

---

## C. System prompt = output style + append_system_prompt -> APPEND_SYSTEM.md — [NEW]

Mirror codex's `_build_developer_instructions` exactly, but write the result to a FILE.

### [EDIT] `libs/mngr_pi_coding/imbue/mngr_pi_coding/plugin.py`
1. `PiCodingAgentConfig`: add
   - `output_style: OutputStyleName | None = None`
   - `append_system_prompt: tuple[str, ...] = ()`
   (identical shapes to `CodexAgentConfig`).
2. Add `_build_append_system(host)` — copy codex's `_build_developer_instructions`:
   ```
   blocks = []
   if output_style is not None:
       blocks.append(resolve_output_style(output_style, read_output_style_files(host, get_shared_output_styles_dir(Path(work_dir)))))
   blocks.extend(append_system_prompt)
   return SEP.join(b for b in blocks if b) or None
   ```
   (reuse `imbue.mngr.agents.output_styles.{read_output_style_files, resolve_output_style}`
   and codex's `get_shared_output_styles_dir` / `DEVELOPER_INSTRUCTIONS_SEPARATOR`.)
3. In provisioning (`_provision*`, after `sync_home_settings` so the sync can't clobber it):
   write the block to `get_pi_config_dir() / "APPEND_SYSTEM.md"` when non-None; remove any
   stale one when None. pi auto-appends it every turn — survives resume, no CLI-arg limits.
   This is the pi analogue of codex writing `developer_instructions` into `config.toml`.
4. Do NOT pass `--append-system-prompt` (the file is the durable channel; the flag is
   per-launch and redundant).

- Q1: append semantics only (`APPEND_SYSTEM.md` appends; `SYSTEM.md` replaces) — an output
  style meant to *replace* pi's built-in prompt can't be expressed. Accept (same as codex).
- Q2: confirm `sync_home_settings` never syncs `APPEND_SYSTEM.md` from the user's real
  `~/.pi/agent` (it copies settings.json + resource dirs only — verified — so safe).

### [NEW] test
Extend `plugin_test.py`: `output_style` + `append_system_prompt` set → `APPEND_SYSTEM.md`
written under the pi config dir with the resolved body; unset → not written.

---

## D. Periodic reminders (pi-system-reminders) — [NEW]

Give pi Claude-Code-style `<system-reminder>` nudges (e.g. re-assert the output-style
persona, tk-step discipline) on a cadence, using the `pi-system-reminders` package
(event-driven: `.ts` files exporting `{on, when({branch,ctx,event}), message, cooldown, once}`,
loaded from `<home>/reminders/` or `.pi/reminders/`).

### [EDIT] `plugin.py` provisioning
1. Install the package into the per-agent dir (so it loads without network at run):
   `pi install npm:pi-system-reminders` scoped to `PI_CODING_AGENT_DIR`, OR vendor its
   loader. Q3: pin the version (mirror the CODEX/PI version pins in `setup_system.sh`).
2. Provision a mngr-owned reminder file `<PI_CODING_AGENT_DIR>/reminders/mngr_reminders.ts`
   (shipped as a plugin resource, like `mngr_pi_lifecycle.ts`) with the nudge(s):
   ```ts
   export default () => ({
     on: ["turn_end"],
     when: ({ ... }) => /* every N turns via a cooldown */ true,
     cooldown: <N turns or ms>,
     message: "<reminder text>",   // rendered as <system-reminder name="mngr">...</>
   })
   ```
- Q4: alternative — inject the reminder inline from the existing `mngr_pi_lifecycle.ts`
  in a `before_agent_start` handler on a turn counter, avoiding a package dependency
  entirely. Recommend this if we only need one or two static reminders; use the package
  if reminders grow. Decide before coding.
- Q5: what reminders do we actually want v1? Likely: keep-in-character (output style) and
  the tk/step + commit-before-stop discipline the chat agents rely on. Keep minimal.

---

## E. Backend harness wireup (reach the UI) — [NEW] `harnesses/pi/`

Mirror `harnesses/codex/`. All in the dwt repo (`system/apps/system_interface`), no
vendored-mngr edits.

- `[EDIT] harness_type.py`: `PI = "pi-coding"`.
- `[EDIT] registry.py`: a `HarnessSpec` for PI (watcher/tracker/resolver/catalog/special_kinds).
- `[NEW] harnesses/pi/model.py`:
  - `PiModelResolver`: `guess_from_launch` = read `pi_model_state.json` (from B); fallback
    to `settings.json` `defaultModel` + declared default thinking; `read_live` = same file;
    `watched_paths` = `(pi_model_state.json,)`; `switch` = write a control file (see below),
    `SwitchMode.ON_CHANGE`.
  - `PI_CATALOG`: pi models (bundled default list + shrug fallback for off-catalog, like
    codex) with efforts = thinking levels.
  - `[EDIT] harnesses/model.py`: add `OFF="off"`, `MINIMAL="minimal"` to `EffortLevel`
    (pi's thinking axis is off|minimal|low|medium|high|xhigh|max).
- `[NEW] harnesses/pi/watcher.py` + `session_parser.py`: tail pi's native session JSONL
  via the `pi_session_file` marker (rotation pointer, exactly like codex's
  `codex_transcript_path`); parse `message` lines → common events; `event_id` = pi entry id.
  `special_kinds = frozenset()` (no turn markers in the JSONL).
- `[NEW] harnesses/pi/activity.py` + `activity_state.py`: `marker_filename` = a new
  `pi_process_started` [EDIT plugin: touch it in `assemble_command` before exec]; derive
  THINKING/TOOL_RUNNING/IDLE from the `active` marker + tail staleness (claude-style
  heuristic; pi has no turn boundaries in the transcript).
- `[NEW] harnesses/pi/tool_labels.py` + `icon.svg`.

### Model changer (switch) — [EDIT] `mngr_pi_lifecycle.ts`
The inbox delivers *user messages*, not slash commands, so `switch()` can't send `/model`.
Add a control channel: the extension watches `<state>/pi_control.jsonl` (same poll pattern
as the inbox) and applies `pi.setModel(ctx.modelRegistry.find(provider,id))` /
`pi.setThinkingLevel(level)`. `PiModelResolver.switch()` appends to that file.

### Exposure — [EDIT] feature flag
Add `FEATURE_FLAG_ENABLE_PI` (server.py meta tag + `/api/harnesses` gate +
`flip_feature_flags.sh` + `CreateAgentModal.ts` launcher) to dark-launch.

---

## Build order (pi)
1. **C** (APPEND_SYSTEM.md + config fields) — unblocks `-t chat`; smallest, highest value.
2. **E model.py resolver + catalog + EffortLevel off/minimal** — reads the pi_model_state.json
   already emitted (B), unit-tested in isolation like codex.
3. **E watcher/parser + activity + `pi_process_started`** — the tab renders the transcript.
4. **registry + enum + icon + feature flag** — the tab goes live, model bar shows (ON_CHANGE).
5. **switch (control file)** and **D reminders** — last.

## Open questions
Q1 append-only prompt. Q2 sync never touches APPEND_SYSTEM.md (verified). Q3 pin
pi-system-reminders version. Q4 package vs inline reminder. Q5 which reminders v1.
