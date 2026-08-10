The `submit-upstream-changes` skill now excludes `system/vendor/mngr/` from upstream template PRs: that subtree is a vendored snapshot of the mngr repo, and mngr changes get their own PR on the mngr repo.

New `references/mngr-changes.md` documents the flow -- iterate directly in the vendored tree, then clone mngr to `.external_worktrees/mngr` on a work branch (never `main`), carry the diff since the last vendor sync commit over (`git apply -p4 -3`), and submit it under mngr's own conventions and code-guardian gates.

The skill-markdown mngr-subcommand guard no longer counts a path ending in `/mngr` (e.g. `git -C .external_worktrees/mngr checkout ...`) as a mngr CLI invocation -- the word after such a path is a git subcommand, not a mngr one.
