Wired pi (`pi-coding`) into the chat UI's model bar (first cut: model bar only, no
transcript yet).

- A "New Pi Agent" launcher creates a pi chat agent; its model bar shows the model pi
  actually started with (read from the state file the pi lifecycle extension writes),
  with pi's per-model reasoning ("thinking") levels as the effort options. No fast
  toggle (pi has no fast tier), and no made-up default -- the bar is logo-only until pi
  records a real model.

- pi's model catalog is parsed from pi's own bundled provider data (~1150 models), so it
  is a searchable picker rather than a fixed list. The picker is a new, separate axis
  from switch behavior: `PickerMode` (list vs search) is orthogonal to `SwitchMode`.

- Effort levels are now free-form strings taken verbatim from each harness's catalog,
  not a fixed enum -- so pi's `off`/`minimal`/... levels appear without special-casing,
  and claude/codex are unchanged in behavior.

- Switching pi's model/effort from the bar is ON_CHANGE: the pick is written to a control
  file the pi extension applies via its own API, and the chip reconciles from the state
  file. (The extension-side consumer of that control file is a follow-up.)
