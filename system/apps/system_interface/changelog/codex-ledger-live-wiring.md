Codex agents now run their message lifecycle through one live app-server connection per agent (the `CodexMessageLedger` over a persistent, thread-bound client with a background notification reader), replacing the fork-era control-file machinery:

- Sending a message to a codex agent submits through the ledger (backend-authoritative send/queue/deliver/return), so queue chips, delivery, and interrupt-return are decided by the daemon's committed turns rather than by tailing a sidecar file.

- The queue chips, the activity dot (RUNNING until the turn actually completes), and the model-bar mirror are all driven by the ledger's live event stream. The old codex activity marker read and the queued-input sidecar tailing are gone.

- The Stop button interrupts a codex turn natively (one interrupt plus a per-message settle that returns every non-committed message to the composer in send order and keeps the committed one), and the shoulder tap is a pure availability gate. The old codex control-line writers (`flush_codex_queue_atomic`, the retract/settle stop path, `CodexQueueTracker`) were removed.

- A codex agent whose daemon dies has its dot settled to idle and its (ephemeral) queue chips dropped, and a fresh session starts with an empty queue.

- Removed the now-inert codex transcript activity tracker (`codex/activity.py`, `codex/activity_state.py`) and made `HarnessSpec.tracker_class` optional: codex activity is driven by the live ledger, so it registers no tracker.
