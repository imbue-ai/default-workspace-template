Codex message tracking now keys on the agent's own committed-message identity and cleanly splits the durable transcript from the live queue, so a message can never briefly appear twice (as a queued chip and a transcript turn at once):

- The live ledger records and reconciles delivery on codex's own app-server `item.id` (adopted when the message commits), not on the frontend-minted id. The minted id is kept only as a correlation token that links the optimistic "Sending..." bubble to the committed message; it is no longer the delivery key.

- The subscribed ledger now owns the live user-turn: when any user message commits -- one you sent, or one typed straight into the agent's terminal, or from another client -- it removes the queued chip first and then emits the transcript turn, in that order, so the two are never shown at once. Foreign (terminal-typed) messages surface in the Minds transcript when they commit, keyed on the agent's identity so nothing is dropped as "not ours".

- The rollout file reader still owns the full committed transcript and all agent output, but no longer emits user turns to the live stream (the ledger does); it keeps them for the page-load/reload rebuild, so hydration is unchanged and the same message is never double-emitted across the two channels.
