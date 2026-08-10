The `submit-upstream-changes` skill now excludes `system/vendor/mngr/` from upstream template PRs: that subtree is a vendored snapshot of the mngr repo, and mngr changes get their own PR on the mngr repo.

New `references/mngr-changes.md` documents the flow -- iterate directly in the vendored tree, then carry the diff since the last vendor sync commit into a standalone mngr checkout (`git apply -p4 -3`) and submit it under mngr's own conventions and code-guardian gates.

New `scripts/create_mngr_checkout.sh` creates that standalone mngr clone at `.external_worktrees/mngr` on a work branch off `origin/main` (named after the current workspace branch, or the explicit argument; it refuses to sit on a base branch).
