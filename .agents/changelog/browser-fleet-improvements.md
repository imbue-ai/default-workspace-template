The `agentic-browser-fleet` skill now notes that, on resume after a human held the browser, the live view may have been resized and the page reflowed -- so every cached element number should be treated as stale and `state` re-run before acting.

The `agentic-browser-fleet` and `manage-layout` skills now tell agents the browser pane is surfaced automatically and must never be opened by hand (`layout.py open browser` / `split browser`) -- a bare `service:browser` has no browser bound and is rejected, and a session-qualified open is redundant with the auto-pane.
