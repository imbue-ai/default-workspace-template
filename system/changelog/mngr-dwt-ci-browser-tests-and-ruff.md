CI runs the chat app's and the shell's browser tests again. Since the suites moved into the workspace image on offload, offload's discovery pass inherited each app's default `-m "not browser and not real_claude"` and dropped every browser test, so none had run in CI. `-m ''` now applies at discovery too, and the group filters leave out only `real_claude`, some of whose tests need a signed-in Claude account.

CI also runs `ruff check` over the root scope, the chat app and the shell, using the locked ruff. Like the changelog gate, it is opt-in through a repository variable (`ENFORCE_RUFF`), which the template sets, so workspace repos made from the template do not start linting their own apps. The pre-commit hook runs that same locked ruff instead of whatever `uvx` resolves.

mngr is pinned to a newer commit whose trailing-comment ratchet exempts a bare `# noqa` (or `# noqa: CODE`) ending its line, as it already exempts `# ty: ignore`.
