- Fixed the flaky proto-agent-logs websocket tests: the `close_ws` test helper now tolerates `OSError` (EBADF) alongside `ConnectionClosed`, closing the race where the client's background thread tears down the socket fd (after the server-side close) between `ws.close()`'s connected check and its close-frame send.

- Pointed the e2e Playwright fixtures at the workspace-provisioned Fortress Chromium (`/opt/fortress/tilion-fortress/tilion`) via `executable_path` when it exists, falling back to Playwright's own managed browsers otherwise, so the e2e suite no longer errors at setup on hosts without Playwright's downloaded chromium headless shell.

- The e2e browsers-installed skip guard now also counts Fortress as an installed browser (shared helper `is_e2e_browser_installed` in `testing.py`), so a Fortress-only fresh workspace runs the e2e suite instead of silently skipping it.

- Dead-code sweep: removed the unused `SwitchMode.ON_CHANGE` / `SwitchMode.READ_ONLY` enum members and the unreachable 409 read-only branch in the set-model-choice endpoint (all three harnesses are `EAGER_THEN_RECONCILE`), removed the dead `setSelectedModelId` compatibility export from `MessageInput.ts`, and corrected the stale switch-mode comments in `ModelSettings.ts` / `ModelBar.ts` / `HarnessCatalog.ts` plus the harnesses-endpoint docstring that named deleted catalog fields.
