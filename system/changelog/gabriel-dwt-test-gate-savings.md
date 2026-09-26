Lint and formatting checks leave the frontend test runs, browser tests leave the default pytest runs of the chat app and the shell, and CI keeps running all of them.

- Each frontend package's `lint` is now eslint alone, and a new `typecheck` script runs `tsc --noEmit` (`system/package.json` fans it out like `lint`). The vitest wrapper that ran eslint and prettier inside `npm test` is gone; under load those checks took 99-161 s against a 60 s timeout and failed runs for reasons unrelated to the change.

- CI runs `npm run lint`, `npm run format:check` and the shared library's `typecheck` after `npm run build`, and runs the chat and shell suites with `-m ''`, so it still covers every test.

- `test_claude_plugin_first_session.py` checks for the claude CLI and an API key before any fixture runs, so a run without them skips at once instead of building a worktree first.

- AGENTS.md says how to pick a change's tests (`app-manifest select-tests`, which refuses to run while an edit is uncommitted) and how to run the chat and shell browser tests, and that a shed test command of a harden gate follows the gate's shed handling and is never skipped.

- New `system/config/test_selection_overrides.toml` holds the test mappings `app-manifest select-tests` cannot derive from the workspace's declarations. A change that leaves a tracked path mapped to no suite fails CI until the file gains a mapping for it.
