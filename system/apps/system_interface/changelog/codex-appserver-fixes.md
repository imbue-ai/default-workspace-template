A codex agent's web-chat transcript no longer renders empty when it has only been driven from the
web (or CLI). The transcript watcher followed a marker file (`codex_transcript_path`) that is
written by codex's `UserPromptSubmit` hook -- and the app-server does NOT fire that hook on a
programmatic `turn/start`, only on a turn typed into the `--remote` terminal. The watcher now
falls back to the newest rollout file under the agent's sessions directory when the marker is
absent, so (with one thread per daemon) the live conversation is tailed regardless of how it was
driven. The marker still wins when present, preserving the deterministic path for TUI-driven turns.
