Lint and formatting checks leave the frontend test runs, browser tests leave the default pytest runs of the chat app and the shell, and CI keeps running all of them.

- Each frontend package's `lint` is now eslint alone, and a new `typecheck` script runs `tsc --noEmit` (`system/package.json` fans it out like `lint`). The vitest wrapper that ran eslint and prettier inside `npm test` is gone; under load those checks took 99-161 s against a 60 s timeout and failed runs for reasons unrelated to the change.

- CI runs `npm run lint`, `npm run format:check` and the shared library's `typecheck` after `npm run build`, and runs the chat and shell suites with `-m ''`, so it still covers every test.

- `test_claude_plugin_first_session.py` checks for the claude CLI and an API key before any fixture runs, so a run without them skips at once instead of building a worktree first.

- AGENTS.md says how to pick a change's tests (`app-manifest select-tests`, which refuses to run while an edit is uncommitted) and how to run the chat and shell browser tests, and that a shed test command of a harden gate follows the gate's shed handling and is never skipped.

- `.mngr/settings.toml` copies `system/vendor/mngr-assets` into every worker's worktree. The assets are fetched rather than tracked, so a fresh worktree had none, and the repo guards failed on the missing `docs/system/style_guide.md` until something ran an npm build. It is a copy rather than a symlink to the workspace's directory: vite resolves a symlink to its target, and vitest refused to load the service icons from outside the worktree, failing 12 of the chat frontend's tests in every worker.

- Tests are marked by what they are, in every pytest configuration: `browser` for one that drives a browser, `frontend` for one that reads an app's frontend source, and `real_claude` for one that runs the real claude binary (`test_claude_plugin_first_session.py` among them). The `release` marker is gone. AGENTS.md and the reviewer's issue categories ask for these markers, which the test gate selects by.
