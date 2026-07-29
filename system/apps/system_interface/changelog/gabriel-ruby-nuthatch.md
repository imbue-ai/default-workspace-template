- New chats now start on fast mode and then ask whether to keep it. After a
  configurable number of user turns (`FAST_MODE_GRACE_TURN_COUNT` in
  `fast_mode_policy.py`, currently 5) a popover appears above the composer's
  lightning-bolt toggle -- the control that answers the same question every time
  after this one. It states the tradeoff in concrete terms -- fast mode is 2.5x
  faster and 6x more expensive -- links to Anthropic's fast mode docs, and points
  at the toggle as the way to change the answer later. "Switch to standard speed"
  is the highlighted default and opens focused, and dismissing the popover --
  backdrop or Escape -- takes it too, so the cheaper outcome is the one nobody
  can pick by accident. The popover falls back to the middle of the screen when
  the toggle is in a hidden panel or has no room above it.

- The answer is recorded for the whole workspace at
  `data/.state/fast_mode_decision.json` and served by
  `GET|POST /api/workspace/fast-mode`. Every chat agent created afterwards
  launches with it and no chat asks again. Chats already running keep their
  current setting until they restart; only the chat that raised the prompt has
  the answer applied live.

- Fixed the composer's fast-mode toggle showing the wrong state. It read
  `fastMode` from the shared Claude `settings.json` alone, which never sees the
  managed `--settings` file mngr passes at launch -- so a freshly launched agent
  provisioned fast displayed the toggle as off. Fast mode is now resolved across
  both layers, with the managed file winning as Claude Code layers it.

- Because Claude Code deletes the `fastMode` key when `/fast` turns fast mode off
  rather than writing `false`, a running session's state cannot be recovered from
  its settings files at all. The system interface therefore remembers what it last
  set for each agent and reports that, so a toggle no longer snaps back when the
  frontend reconciles after a change settles. That record is per-process, is
  dropped when the agent is destroyed, and is empty after a restart of the
  service, which falls back to the launch-time value.

- Two consequences of that record worth knowing: it describes a session, so an
  agent that restarts on its own is back at its launch-time setting while the
  record still reports the last toggle; and a `/fast on` that Claude Code refuses
  is displayed as on, since there is no longer anything on disk to reconcile
  against. This workspace disables the org-level eligibility check that is the
  thing that would refuse one.

- `read_model_settings` is now `read_model_from_settings` and returns only the
  model; fast mode is resolved separately, since unlike the model it cannot be
  read from one file.
