- Restyled the in-chat permission-request card: a muted "Permission request" eyebrow now tops the card, followed by a lock badge beside a short title ("Local files", "Other machines", "Device accounts", or the resolved service name) with the agent's rationale directly beneath it. The pending card pairs a solid green "Review & respond" button with a plain-text "Show raw request" toggle, and resolved requests render as a compact one-line receipt ("Approved" in green, "Denied" in neutral, "Couldn't complete" in amber).

- The card no longer lists the specific requested permissions inline (the old "Requesting" line and its hover tooltips); those details live in the review modal and the raw-request disclosure.

- The card flips to Approved/Denied the moment the request is resolved in the Minds app's review popup, rather than waiting for the resolution to come back through the agent transcript. The verdict arrives over the minds embed contract (the same postMessage path in the desktop app and in a plain browser), which admits it only from this page's own embedder and only with a well-formed payload; the transcript's own resolution still wins once it lands.

- All messaging with the embedding Minds app now goes through that shared contract module rather than hand-rolled postMessage calls, so both ends of the boundary ship from one source of truth.
