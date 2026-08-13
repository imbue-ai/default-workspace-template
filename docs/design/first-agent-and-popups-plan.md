# First-agent template + harness popup commonization — plan

Status: PLAN (2026-08-11). A separate track from the codex app-server build. **Runs AFTER the codex
build lands** — it edits central files the codex build also touches (`registry.py`,
`agent_manager.py`, `server.py`) and it needs codex's `minds_model_state.json` writer for the real
fast-tier signal. Not parallelizable with the codex build.

Grounded in the exploration earlier this session: the create-template + `_apply_template_contributed
_settings` mechanism, the `HarnessSpec` registry (`system_interface/harnesses/registry.py`), the
three claude-specific popups, and the `WorkspaceFastMode` / `fast-mode-prompt` system.

## Goal

Three things, one theme (commonize per-harness UI machinery that today is claude-shaped):
1. Decouple the "first agent" from the bootstrap's hardcoded `--type claude` + append-fast-mode +
   `--message /welcome` into a reusable `[create_templates.first]` selected by `-t first`.
2. Stop defaulting every claude agent to fast mode; make fast mode a `[first]`-only intent that is
   benign to the wrong harness and to an unsupporting account.
3. Fold the three scattered claude popups into one `HarnessPopup` system on the dwt `HarnessSpec`,
   and simplify the keep-fast-mode prompt to a single honest per-first-agent question.

## Part 1 — the `[first]` template + fast-mode-as-intent

- Add `[create_templates.first]` with `parent`/`parent_type = "chat"`, `first_message = "/welcome"`,
  a `role=first` **label** (mngr's real tagging mechanism is `get_labels()/set_labels()` —
  `dict[str,str]`, NOT a separate "tags" system), and **namespaced** fast settings contributed via
  `_apply_template_contributed_settings`:
  - `agent_types.claude.settings_overrides.fastMode = true`
  - codex's equivalent (its `serviceTier = "priority"` setting).
  A claude agent reads only the claude namespace, codex only its own — each **benign** to the other
  by construction; pi sets neither. An account without the priority tier makes it a no-op (claude
  corrects via the model bar; codex's `thread/settings/update` degrades via warning+fallback).
- **Remove `fastMode` from the `agent_types.claude` settings_overrides** (no more fast-by-default).
- **Bootstrap:** the services agent creates the first chat agent with `--type claude -t first`
  (replacing `--type claude` + `-S ...fastMode=...` + `--message /welcome`). Leave the
  `<synthetic>` /welcome-when-unauthenticated system alone (too much UX rework to change now).
- Confirm a create-template can contribute a **label** the same way it contributes settings (the
  one mechanical check flagged during exploration).

## Part 2 — `HarnessPopup` on the dwt `HarnessSpec`

Put it on `system_interface/harnesses/registry.py::HarnessSpec` (dwt — that is where `switch()`, the
catalog, tap, and activity already live; there is NO mngr-side HarnessSpec). A `HarnessPopup`
declares `{trigger, title, body (must include the agent's name), buttons -> actions}`. The harness
declares the *facts*; system_interface renders the popup. Three families, replacing today's
scattered claude-only code (`MessageInput.ts` + `claudeSlashCommands.ts` + `ClaudeLoginModal.ts` +
`FastModeModal.ts`):

1. **Blocked / terminal-only commands** — claude's declined slash list AND codex's ex-fork
   blocklist (the slash-lockdown the fork used to enforce, now gone). Popup: "‹cmd› changes
   ‹agent›'s terminal — run it in the terminal." Each harness contributes its own blocked set.
2. **`/login` (and `/logout`) special-handle** — its own popup routing to the managed sign-in.
3. **Keep-fast-mode** — see Part 3.

## Part 3 — simplify keep-fast-mode; delete `WorkspaceFastMode`

Once fast mode is `[first]`-only, only the first agent is ever auto-fast, so the workspace-global
decision is dead weight. **Delete** `WorkspaceFastMode.ts`, the `GET/POST /api/workspace/fast-mode`
(server.py, models.py), `launch_defaults.py`'s decision read/write + `FAST_MODE_BEFORE_DECISION`,
and the `agent_manager.py` per-agent `-S ...fastMode=<decision>` propagation.

Replace with one `HarnessPopup` (keep-fast-mode) that fires only when, for a SINGLE agent:
- it carries the `role=first` label, AND
- it is **actually** in the fast tier — read the effective `fast` from `minds_model_state.json`
  (claude: the statusline's real `fast_mode`; codex: the ledger's write of
  `serviceTier=="priority"`), NOT the `fastMode` setting (that is the false-trigger bug), AND
- it is idle, AND past ~5 user turns.
"Turn off" → the existing `switch()`. "Keep on" is a **no-op / re-affirm** (never newly enables). No
workspace persistence, no effect on any other agent.

**The load-bearing new bit:** detecting the *actual* fast tier vs. the setting. `minds_model_state.
json`'s `fast` field is exactly that signal for both harnesses (claude via statusline, codex via the
ledger writer built in the codex build) — so this track depends on that writer existing.

## Sequencing + conflict notes

- Runs AFTER the codex build (depends on its final `registry.py`/`agent_manager.py`/`server.py`
  shape and the codex `minds_model_state.json` writer).
- Touches: `.mngr/settings.toml`, the bootstrap/services-agent launch, `registry.py` (HarnessSpec +
  the per-harness blocklists/popups), the mngr claude/codex/pi plugins only if a blocklist needs a
  harness fact, and the frontend popup layer (`MessageInput.ts`, `FastModeModal.ts`,
  `WorkspaceFastMode.ts` → deleted, `claudeSlashCommands.ts`, `fast-mode-prompt.ts`).
- Every phase tested + committed; the contract does not change here (popups are UI affordances, not
  message-lifecycle states) — but the keep-fast honesty rule (only prompt when actually fast) is the
  one behavioral invariant to assert.
