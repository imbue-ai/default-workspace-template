# Submitting mngr changes

`system/vendor/mngr/` is a vendored snapshot of the mngr repo, refreshed by
periodic sync commits (`system/vendor/mngr: refresh from mngr <sha>`). Editing
it directly is a fine way to test an mngr change in the running workspace, but
it is never the way to ship one: the next vendor sync overwrites direct edits,
and this repo's PRs are not where mngr code gets reviewed.

**The rule: changes under `system/vendor/mngr/` do not go in an upstream
template PR. They get their own PR on the mngr repo.** Vendor syncs from mngr
main happen frequently upstream, so once the mngr PR merges, the template
picks the change up automatically -- the template PR usually does not need to
carry any mngr content at all.

## Flow

1. Iterate directly in `system/vendor/mngr/` until the change works in the
   running workspace. Committing those edits to the workspace repo as you go
   is fine (and keeps the clean-tree gate happy); they just won't be part of
   the upstream submission.

2. Once satisfied, create a standalone mngr checkout:

   ```bash
   bash .agents/skills/submit-upstream-changes/scripts/create_mngr_checkout.sh <branch-name>
   ```

   This clones mngr to `.external_worktrees/mngr` and creates `<branch-name>`
   off `origin/main` (defaulting to the current workspace branch name; it
   refuses to sit on main). The checkout is a normal mngr clone: mngr's own
   committed code-guardian policy applies, and `.reviewer/settings.json` here
   lists the directory under `stop_hook.additional_git_directories`, so the
   stop hook reviews work there alongside the workspace once it exists.

3. Carry your changes over. Diff the vendored tree against the last sync
   commit (whose message records the mngr sha it vendored):

   ```bash
   sync_commit=$(git log -1 --format=%H --grep 'refresh from mngr' -- system/vendor/mngr)
   git diff "$sync_commit" HEAD -- system/vendor/mngr > /tmp/mngr-changes.patch
   git -C .external_worktrees/mngr apply -p4 -3 /tmp/mngr-changes.patch
   ```

   (`-p4` strips `a/system/vendor/mngr/`; `-3` falls back to a three-way merge
   when mngr main has moved past the vendored base. Include uncommitted vendor
   edits with an extra `git diff -- system/vendor/mngr` if you have any.)
   Review the applied result -- you are reconstructing intent, not blindly
   porting bytes.

4. Commit in the checkout and follow mngr's own conventions from there (its
   CLAUDE.md governs; unlike this repo, mngr expects a draft PR on the mngr
   repo, and the code-guardian gates on the checkout will hold the stop until
   the branch is pushed and reviewed).
