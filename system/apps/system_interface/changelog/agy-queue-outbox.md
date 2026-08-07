# agy queued messages: a send-sourced outbox feeding the shared queue

agy now slots into the shared shoulder-tap queued-message system. It has no on-disk
enqueue ledger (no `pi_inbox` analogue, and a parked message does not reach its
transcript until it drains), so its populator's enqueue source is the UI's own send:
the send endpoint calls a new base-watcher hook `note_sent_message` (no-op default,
only the agy watcher overrides it) and the populator holds the outbox of sends not
yet drained.

agy coalesces its queue (N parked messages drain as ONE newline-joined turn), declared
as `HarnessCatalog.queue_behavior = COALESCES` (new enum, default `NORMAL`, inert for
every other harness). The populator's `leave` resolves a drained turn by verbatim
front-run matching against the outbox's own stored contents -- it pops exactly the
entries the turn joins, so multi-line messages and duplicates resolve correctly, and a
turn typed straight into agy's terminal matches nothing and pops nothing. The
working->IDLE backstop sweeps stragglers; snapshot pushes are debounced (2s) so an
idle-agent send never flickers as queued.
