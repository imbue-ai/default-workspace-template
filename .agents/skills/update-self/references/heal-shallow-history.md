# Healing a shallow-history workspace

Workspaces created from a pool host baked before the full-history bake fix
carry a `--depth 1` clone: `git log` dead-ends at a parentless graft commit,
`git describe` fails, and "what changed since the fork point" is unanswerable.
The upstream template is public, so completing the history is one fetch. Both
the lead (before resolving the target) and the worker (before its merge) run
the same guarded snippet, which is a no-op on a healthy repo (`--unshallow`
errors on a repo that is not shallow):

```bash
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
```

`--git-common-dir`, not `--git-dir`: the shallow marker is repo-wide state that
lives in the common dir, which is what a worktree checkout shares -- so the
worker's run heals the whole workspace, not just its worktree. The snippet
ships in the target version's copy of the flow, so the heal applies on the
first update into the release that carried it.
