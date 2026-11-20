# Captured grok session logs

Real `updates.jsonl` session logs written by the pinned Grok Build CLI, **grok 1.0.62**, driven
by a stub model server. Each scenario directory holds one `<session id>/updates.jsonl` per session
and a `roots` file naming its top-level sessions; any other session directory is a subagent's
child session.

- `sessions/` is a verbatim copy of mngr's `libs/mngr_grok/imbue/mngr_grok/resources/test_fixtures/sessions/`
  at the mngr commit `pyproject.toml` pins, captured in grok's TUI. Its own
  `README.md` lists the scenarios. mngr's transcript converter is tested on the same files.
- `tui_sessions/` was captured for the chat app in grok's TUI, on a private tmux server:
  `subagent_ends_mid_reply` (an agent made by mngr with the template's hooks: a background
  subagent, then a slow reply during which it ends, which grok writes as two text lines, then the
  wake turn; root and child sessions) and `resume_after_quit` (grok quit with Ctrl+Q during a
  foreground tool, then relaunched with `--resume`, which closes the turn as `interrupted`).
  Nothing in them names the capture machine, so they are copied unchanged.
- `headless_sessions/` was captured for the chat app's parser with `grok -p` against a stub that
  replays scripted tool calls: `exit_codes` (non-zero exits, a missing file, a command moved to
  the background), `signal` (a command killed by a signal), `stamps` (a latchkey permission
  request, a secret request and tk lines echoed by shell calls, and a request echoed after output
  grok truncated), `streaming` (text before a call, a thought, a 600-word run streamed over 33 s),
  `tools` (each workspace tool once, a monitor's event and end wakes, and the session's
  `tool_definitions.json`), and `guard_refusals` (calls the template's own `.grok/hooks/policy.json`
  refused, beside ones it let through). Capture paths are rewritten to
  `/tmp/grok-fixtures/headless/<scenario>`, in byte arrays as well as strings, and the agent and
  host ids in `guard_refusals`' commit identity to zeros.

The parser tests read these files as they are; edit nothing by hand. Re-capture with the
procedure in `docs/design/grok-transcript-schema.md`.
