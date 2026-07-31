- New chats now start on fast mode and then ask whether to keep it. After a
  configurable number of user turns (`FAST_MODE_GRACE_TURN_COUNT` in
  `fast-mode-prompt.ts`, currently 5) a modal states the tradeoff in concrete
  terms -- fast mode is 2.5x faster and 6x more expensive -- links to Anthropic's
  fast mode docs, and points at the composer's lightning-bolt toggle as the way to
  change the answer later. "Switch to standard speed" is the highlighted default
  and opens focused, and dismissing the modal -- backdrop or Escape -- takes it
  too, so the cheaper outcome is the one nobody can pick by accident.

- The answer is recorded for the whole workspace at
  `data/.state/fast_mode_decision.json` and served by
  `GET|POST /api/workspace/fast-mode`. Every chat agent created afterwards
  launches with it and no chat asks again. An unanswered workspace is the file
  being absent, so there is no "decided" flag that can disagree with the value.
  Chats already running keep their current setting; only the chat that raised the
  prompt has the answer applied live.

- Fixed the composer's fast-mode toggle showing the wrong state. It read
  `fastMode` from the shared Claude `settings.json` alone, which never sees the
  managed `--settings` file mngr passes at launch -- so a freshly launched agent
  provisioned fast displayed the toggle as off. Fast mode is now resolved across
  both layers, with the managed file winning as Claude Code layers it.

- A fast-mode change made through the UI is now recorded in the agent's own launch
  settings, not just sent to the running session. Claude Code deletes the
  `fastMode` key on `/fast off` rather than writing `false`, so the session leaves
  no usable record of its own state; writing to the file mngr passes as
  `--settings` (or, for an agent with its own config dir, that dir's
  `settings.json`) means the toggle reads back correctly *and* an agent that
  restarts comes back on the setting the user chose rather than reverting to the
  one it was provisioned with. The write patches the single key, since mngr's hooks
  share that file, and refuses to touch a file it cannot parse -- reporting a 500
  rather than silently dropping those hooks.

- A `/fast on` that Claude Code refuses is still displayed as on, since there is
  nothing on disk to reconcile against. This workspace disables the org-level
  eligibility check that is the thing that would refuse one.

- `read_model_settings` is now `read_model_from_settings` and returns only the
  model; fast mode is resolved separately, since unlike the model it cannot be
  read from one file.
