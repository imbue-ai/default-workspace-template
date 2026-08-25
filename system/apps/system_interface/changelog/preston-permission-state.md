# Permission cards stop showing stale or swapped verdicts

Fixed the in-chat permission cards' two ways of lying about a request's state, per the consolidated diagnosis in mngr's `specs/permission_state.md`.

Verdicts now correlate strictly by request id: the resolution notice minds sends carries the resolved request's own id, and the timeline walk pairs each verdict with the card that owns it, so out-of-order resolutions no longer swap Approved/Denied badges and a message batching several requests resolves each card independently. The old arrival-order guess -- the very mechanism that produced the swaps -- is deleted rather than kept as a fallback; a notice with no id (pre-dating id embedding) attributes nothing, and embedded pages recover such verdicts through hydration instead.

Cards also hydrate on page build: a card that renders undecided queries the embedding chrome (embed contract v3) and records the answer, drawn from the desktop client's durable response log, so a reloaded page never offers Approve and Deny for an already-decided request. The same `minds:permission-resolutions` message carries the instant flip when the user resolves a request live, replacing the v2 push type. Without an embedder, or with one predating v3, cards keep the previous transcript-driven behavior.
