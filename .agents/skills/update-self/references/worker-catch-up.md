# Worker: catching up, and retrying after a rollback (the worker guide's Steps 7 and 8)

Read this when the lead replies asking you to catch up to its `HEAD`, or to
retry after its apply rolled back.

## Contents

- Catch up when the lead's `HEAD` moved
- Validate the delta
- Report `done` again
- Retry after the apply rolled back

## Catch up when the lead's `HEAD` moved

The apply lands your branch only by fast-forward, so anything another chat
committed after you branched makes it refuse. Your validated merge stands:
bring the workspace's new commits into your branch and validate only what
they change.

```bash
rm -f data/.tasks/update-self/catchup-impacted.txt
git merge --no-ff <the commit the lead named> -m "Catch up to the workspace's latest work (<its short sha>)"
```

Never give this merge, or anything else you commit, an `update-self:`
subject: the apply and `footprint-ranges` find this pass's merge by that
prefix, and your `update-self:` merge must stay on the branch's first-parent
line, which merging the lead's commit into your branch (not the other way
round) keeps.

Resolve conflicts by `worker-conflict-triage.md`, with two differences:

- The commits coming in are the workspace's own latest work, so where they
  and the update meet, keep both.
- The sides are swapped: `--ours` is your branch (the update) and `--theirs`
  the workspace, so the triage's "keep local" is `git checkout --theirs`
  here.

Regenerate a lockfile, never merge it, and do so whenever both sides changed
it even without a conflict. Conclude a conflicted merge with `git commit
--no-edit`, which keeps the subject the suites step finds it by.

## Validate the delta

Scoped by the same rules as the worker guide's Step 4b, but over
`$PRE..HEAD` only, where `$PRE` is your branch's tip before the catch-up
merge (that merge's first parent). Do not re-run 4b's recipe: its merged set
re-selects the whole pass, and its `git log "$MERGE..HEAD"` line would read
the workspace's commits as your edits.

- **Impact**: when the commits coming in add or change user-created code,
  check it against what the update changed. Search it for the names the
  update retires (`worker-impact-analysis.md`, section 2), and for a use of a
  file the update changed: the dependents search and the interface coupling
  of that file's section 3 (its items 2 and 3). List each impacted creation
  in `data/.tasks/update-self/catchup-impacted.txt`, one stand-in file per
  line, as `impacted-paths.txt` does.
- **Environment gate** when a manifest or lockfile changed in `$PRE..HEAD`.
- **Suites**: commit first, then run every printed line:

  ```bash
  PRE=$(git rev-parse "$(git log --first-parent --merges -1 --format=%H --fixed-strings \
      --grep "Catch up to the workspace's latest work")^1")
  { git diff --name-only --no-renames "$PRE" HEAD
    cat data/.tasks/update-self/catchup-impacted.txt 2>/dev/null; } \
      | sed '/^[[:space:]]*$/d' | sort -u > data/.tasks/update-self/catchup-paths.txt
  [ -s data/.tasks/update-self/catchup-paths.txt ] \
      && uv run --frozen --package app-manifest app-manifest select-tests \
          --diff-base "$PRE" \
          $(sed 's/^/--path /' data/.tasks/update-self/catchup-paths.txt)
  ```

  Handle a failing test as 4b does, except that the tree before your change,
  for attributing a failure, is the commit you merged (the catch-up merge's
  second parent), not `$MERGE^1`: a test that fails there was already failing
  in the workspace, and one absent there arrived with the update.
- **Isolated-service boots** for a service with a file both sides of the
  catch-up merge changed (your branch and the incoming commits, conflicted or
  auto-merged), as 4b boots its merged set, and for each service that carries
  local content of its own with a file in `catchup-impacted.txt`, as 4b boots
  what 4a found impacted. Any other service only the workspace changed is
  already live in that state.
- **Playwright** for a web surface with a file you resolved a conflict in, as
  4b drives it.
- **Bundles**: rebuild all seven when you reported them and `$PRE..HEAD`
  touches what they are built from: the directory of any of the seven apps
  (`system/apps/system_interface/`, `system/apps/chat/`,
  `system/apps/getting_started/`, `system/apps/files/`,
  `system/apps/routines/`, `system/apps/screen/`,
  `system/apps/style_guide/`), `system/libs/workspace_ui/`,
  `system/package.json`, or `system/package-lock.json`.
- **Customization survival** for a creation the update touches (the scope
  files) whose files the incoming commits also change.
- **Review gates**: a conflict you resolved here is merge work, in 4c's
  scope. The catch-up merge itself is not an edit of yours.

## Report `done` again

With the full body of `worker-report.md`, brought up to date, and a
**Catch-up** section first: the commit you merged, the commits it brought in,
each conflict and its resolution, and which of the items above ran, with
their results.

## Retry after the apply rolled back

When the lead's apply of your branch rolls back, the lead deals with the
cause (a loaded workspace, a registry it could not reach) and asks you to
retry. The workspace's `HEAD` now holds your branch, a `Roll back update
apply` commit on top of it, and whatever was committed since. Bring that in
as a catch-up does (merge the commit the lead named), then revert the
rollback on your branch with the `pending-rollbacks` block of the worker
guide's Step 1, so the branch holds the update again on top of the
workspace's latest work.

Validate as "Validate the delta" does, over `$PRE..HEAD`: the rollback and
its revert cancel out, so that range is the workspace's commits since your
last `done`. Report `done` again with the Catch-up section, naming the
rollback you reverted.
