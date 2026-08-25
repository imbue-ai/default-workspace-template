# Permission cards stop showing stale or swapped verdicts

Fixed the in-chat permission cards' two ways of lying about a request's state, per the consolidated diagnosis in mngr's `specs/permission_state.md`.

Verdicts are now correlated by request id instead of arrival order: the resolution notice minds sends carries the resolved request's own id, and the timeline walk resolves each card by looking its id up rather than assuming requests resolve in creation order. Two requests resolved out of order (denying a newer one while an older one's grant is mid-OAuth) no longer swap their Approved/Denied badges, and a message batching several permission requests resolves each card independently. The old arrival-order guess remains only as a fallback for transcripts recorded before id embedding shipped, marked for removal.

Cards also hydrate their verdicts from the shell on page (re)build: a card that finds itself undecided asks the embedding minds chrome (new embed-contract query pair, v3) whether a verdict was recorded while the page was not live, and flips to the answer from the desktop client's durable response log. A reloaded page no longer offers Approve and Deny for a request the user already decided while waiting for the agent transcript's own resolution message. Without an embedder, or with one predating the query, cards keep the previous transcript-driven behavior.
