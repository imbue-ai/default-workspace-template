# Harness roadmap: the spine, Codex improvements, and adding harnesses

This is the working plan for the `harnesses-dwt` line of work (PR #385). The north
star is unchanged: **commonize everything possible into a shared spine, keep
harness-specific code contained to `harnesses/<h>/`, and add more harnesses (Codex,
Pi, OpenCode, Antigravity) with minimal per-harness subclassing.**

Each item below is tagged with a **confidence** marker:

- **[READY]** — fully understood, safe to execute.
- **[OPEN]** — needs a decision or more investigation before touching; do NOT guess.
- **[DEFER]** — explicitly out of scope for now.

Manager-side (mngr) changes land on **`imbue-ai/mngr-internal` PR #288**, not here.

---

## Already shipped on `harnesses-dwt`

For context, these are done and green:

- Data-driven model bar (`[Logo][Model][Effort][Fast]`) for Claude (switchable) and
  Codex (read-only v1).
- Codex transcript parser (schema-drift fix for the new `item_completed` shape).
- Model-bar row moved below the composer; "Open agent terminal" + "Agent auth" buttons.
- Delta model switching, corrected to diff against the **optimistic value the user
  saw** (fixes the `medium -> xhigh -> medium` drop); axes computed on the frontend and
  sent explicitly, applied by `switch(identity, axes, send)`.
- `/effort` filtered from chat; user-message classification generalized to one shared
  jungle-gym (`USER_MESSAGE_DETECTORS`); backend non-turn-tail signal list de-Claude'd.
- Claude-only composer/fast-mode prompts gated on `agent.harness == "claude"` (so
  `/goal`, `/login`, and the fast-mode modal no longer fire on Codex).
- Model API errors surfaced in chat: backend classifies (`error_patterns.py`) and stamps
  `is_api_error` / `api_error_kind` / `is_provider_fault`; frontend renders light-red with
  a "not Minds' fault" note for provider faults (5xx / overloaded).
- Three commonization dedups: `parse_effort_level` shared in `model.py`; tool preview/output
  caps shared in `events.py` (`MAX_TOOL_INPUT_PREVIEW_LENGTH` / `MAX_TOOL_OUTPUT_LENGTH`);
  the Codex watcher now runs on the shared `PathWatcher`.

---

## The spine (the load-bearing change; prerequisite for Pi and OpenCode) [READY]

The store today is an **accumulator**; it must become a **materialized view keyed on
stable harness ids**. The client half already exists and is tested — this is a
backend-side change plus a stable-id rule.

1. **Stable event ids.**
   - Claude already complies: every id is `_make_event_id(uuid, suffix)` — derived from
     Claude Code's own message UUID.
   - Codex uses codex's own `msg_id` / `call_id` where available and a content-hash for
     user bubbles, but falls back to a **line-index counter** (`codex-{line_index}-…`) for
     the three turn markers (`turn_started` / `turn_completed` / `turn_aborted`).
   - **Do:** derive marker ids from codex's `turn_id`, not the line index. *Confirm the
     turn payloads carry a `turn_id` before relying on it.*
   - **Write the rule as a comment in the shared layer** (`session_watcher.py` / `events.py`):
     an `event_id` must be the harness's own stable id (Pi's 8-char entry id, OpenCode's
     `prt_`, Codex's `turn_id`-derived key), never a counter — a counter makes truncation
     inexpressible and gives a re-added entry a new id.

2. **D6 + `set_events` — the linchpin.** `codex/watcher.py:315` today does
   `if event_id in self._event_index: continue` (skip duplicates), which keeps a **stale**
   copy when codex re-writes an event with updated content (supersession). Change it to
   `self._events[idx] = event` (replace in place), and give the store a `set_events(list)`
   contract — a view of the current state, not an accumulator.

3. **B1 — `_partial` must be `bytes`, not `str`.** A UTF-8 char split across a read
   boundary corrupts/drops the line. Buffer bytes, decode after splitting on `\n`. This is
   the read path the store sits on; **do it here** (it is a data-loss fix, not deferred).

4. **D5 — reset on connect.** On (re)connect the backend replays the whole backlog as a
   stream of fake "new event" appends. With the view, send one snapshot instead:
   `reset(events, offset, total)` — the client already implements this
   (`Response.ts:393`, tested) and distinguishes replace-vs-append.

5. **C4 + D7 fall out of the view** — the append-only marker becomes a property of the
   view, and the stat/newline handling is correct once the read path is bytes and the store
   is a view. No new mechanism.

---

## Codex model switching v2 (unblocked by the patched CLI) [READY]

**`minhtrinh-imbue/codex-slash-model` release `v0.146.0-modelargs`** patches codex 0.146.0
so `/model <model> [effort]` works in one command (upstream silently ignores the inline
args and sends them as chat — which is why our `switch()` is `READ_ONLY`). Assets:
`codex-linux-arm64`, `codex-linux-amd64`, `SHA256SUMS`. glibc, built on `rust:1-trixie`,
compatible with the `python:3.12-slim-trixie` workspace image.

1. **Wire the binary into the image.** In `system/scripts/setup_system.sh` (NOT a Dockerfile
   — this is the image/VM setup script), replace the npm-vendored codex install
   (`setup_system.sh:236`, `npm install -g @openai/codex@${CODEX_VERSION}`) with a `curl`
   download of the patched binary + **SHA256 verification** (atomic download-to-temp-then-move,
   locate the vendored binary path dynamically). Add version pins near line 39; keep
   `CODEX_VERSION` in sync with `agent_types.codex.version` in `.mngr/settings.toml`, and
   only to a codex version we have a patch for (`patches/0.146.0.patch`).
   - *Note:* `build.sh` builds on EC2 (needs authenticated AWS); the release is already built,
     so we only download + verify.

2. **Flip and implement switching.** Change `CODEX_CATALOG.switch_mode` off `READ_ONLY` (to
   `ON_CHANGE`, which is defined and now gets its first real wiring) and implement
   `CodexModelResolver.switch(identity, axes, send)` to send `/model <id> <effort>`. Uses the
   same axes contract as Claude.

---

## Truncation pass (B2 + B3), both harnesses [READY]

Builds directly on the shared caps just landed (the relevant one is the 200-char
`MAX_TOOL_INPUT_PREVIEW_LENGTH`).

- **B2 — exempt tk and patch outputs from the cap, both harnesses.** Claude already exempts
  tk (`_is_tk_lifecycle_call`, `session_parser.py:345`) — add the **patch** exemption. Codex
  exempts nothing — add **both** tk and patch. *("patch" = a diff/patch body, e.g. codex's
  `apply_patch`; confirm the claude-side patch tool this maps to before exempting it there.)*
- **B3 — label first, truncate second.** Codex has the bug: `_tool_call_input_preview`
  truncates, then `_labelled_tool_call` labels off the **truncated** string. Label off the raw
  input, then truncate for the preview.

---

## Small standalone Codex bugs (independent, no spine) [READY]

- **B4 — `is_error` from `Script failed`.** A failed code-mode script renders as a normal
  successful tool result. Set `is_error` when the `custom_tool_call_output` body starts with
  `Script failed`.
- **B5 — synthetic `Interrupted.` tool_result.** Interrupting mid-tool leaves a tool call with
  no result forever (a spinner that never resolves). On interrupt, synthesize a terminal
  `tool_result` with body `Interrupted.`
- **B6 — tk gating for Codex.** Codex runs tk as `exec_command({cmd: "tk create --step …\n…"})`,
  rendered as `Tool: Bash`. Claude hides the tk tool-call block (the step timeline renders it
  instead); Codex needs the same. **Keep it simple: case it exactly the way Claude does, off the
  `cmd` parameter of `exec_command` — mirror how `tool.<fn>` extraction already works. Missing a
  few edge cases is acceptable** (multi-command `cmd` bodies with newlines exist; a leading-tk
  match is fine).

---

## Codex config: disable unused tools/features [READY]

Add to `[agent_types.codex].config_overrides` in `.mngr/settings.toml` (dumped verbatim into
codex's config.toml, same pattern as the existing `features` sub-table at line 216): under
`tools`, set `update_plan`, `experimental_request_user_input`, and `web_search` to
`{ enabled = false }`; under `features`, add `goals = false`. Maps to codex's
`[tools.*] enabled=false` + `[features] goals=false`.

---

## Deferred

- **Pi harness** [DEFER] — rides the spine once it lands. Not this pass.
- **B7 — `mngr create` conflict guard** [DEFER, do after] — err when stacked templates + `--type`
  resolve to conflicting base types (the guard exists at `create.py:739` for `--type` vs
  positional; extend it to stacked templates). Ambiguity here is always a mistake. **Manager-side:
  lands on mngr-internal PR #288.**
- **B8 — agents-by-id** [DEFER] — address agents by id, not name, in the system-interface
  endpoints. No behavioural change; nothing in the current lanes hard-depends on it.

---

## Open items (bailed — need a decision, do NOT guess)

- **Provisioning `uv sync` revert.** The branch added a **base-level**
  `extra_provision_command__extend = ["uv sync --all-packages"]` at `.mngr/settings.toml:19`,
  which main does not have (main's first provision command is ~line 346). This makes **every**
  provisioned agent run `uv sync --all-packages`. Intent was "put it back… in the worker" —
  unclear whether that means delete it or move it into the worker template, and settings.toml
  template-stacking is subtle enough that a wrong edit could break provisioning for every agent
  type. **Needs your call: delete, or move to `[agent_types.worker]`?**

---

## Sequencing

1. Housekeeping — codex config tool-disable (READY). *(uv-sync revert is OPEN.)*
2. Codex switching v2 — wire the patched binary + flip/implement `switch()`.
3. The spine — stable marker ids + rule comment → D6/`set_events` → B1 → D5 → C4/D7.
4. Truncation pass (B2/B3).
5. Small bugs (B4, B5, B6).
6. B7 (after), on mngr-internal #288. Pi after the spine.

Each lane is its own commit, tested green, before the review gate runs on `harnesses-dwt`.
