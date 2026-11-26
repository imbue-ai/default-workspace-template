# When an update waits

What to do when `app-install update` stops before the new version is live: a
`conflict`, a `tests_failed`, or a `collision`.

## Contents

- The waiting update
- Giving an update up
- A version made for a newer workspace

## The waiting update

A `conflict` or a `tests_failed` leaves the update in its worktree, a second
checkout of the workspace at `data/.state/app_install/worktrees/<name>`. The
app's files are under `system/apps/<package>/` inside it. Nothing the user
runs has changed.

- **A conflict** is a place where the publisher and this workspace changed
  the same thing. Read both sides in each file `conflicted_paths` names. Where
  the right answer is clear (the publisher renamed what this workspace
  restyled), resolve it yourself. Where it is a choice between the user's own
  change and the publisher's, ask the user, in their terms: what they had
  changed, what the new version does there instead, and which they want.
  Then, in the worktree, stage the resolved files and conclude the merge:

  ```bash
  git -C data/.state/app_install/worktrees/<name> add -- <the resolved files>
  git -C data/.state/app_install/worktrees/<name> commit --no-edit
  ```

- **Failing checks** are fixed in the worktree's copy of the app and
  committed there the same way (`git ... commit -m "<what you fixed>"`). A fix
  you make is a change of this workspace's to the app, like any other.

Then finish the update:

```bash
uv run --no-sync app-install update <name> --continue
```

It answers with the same results as the update itself (SKILL.md, step 3). `conflict` again, with a
`detail`, means the worktree is not ready: a resolution that is not committed
yet, or a merge that was taken back there (`git merge --abort`), which leaves
no new version to finish; give that one up and start again. `refused` with a
`detail` saying the result does not merge into the primary checkout means the
workspace changed the same lines while the update waited: merge the
workspace's branch into the worktree
(`git -C data/.state/app_install/worktrees/<name> merge "$(git branch --show-current)"`),
commit, and run `--continue` again.

## Giving an update up

```bash
git worktree remove --force data/.state/app_install/worktrees/<name>
```

The app the user runs was never touched, and the next `update` starts
afresh. Do this when the user decides against the new version, when a
conflict or a failing check is not worth settling, and before starting over
after a `collision`.

## A version made for a newer workspace

Nothing checks up front that a new version suits this workspace. When its
checks fail (`tests_failed`), or it is `updated` and then does not start,
compare what it was made on with what this workspace is on:

```bash
grep verified_template data/.state/app_install/worktrees/<name>/system/apps/<package>/app.toml   # the waiting version
grep verified_template system/apps/<package>/app.toml                                            # after `updated`
git describe --tags --abbrev=0 --match 'minds-v*' HEAD                                           # this workspace
```

`verified_template` is the workspace version the publisher ran when they
published; it is guidance, and an app without the line says nothing. When it
is newer than this workspace's, the likely cause is that the app expects a
newer workspace: do not patch the app to fit. Tell the user the new version
was made for a newer workspace than theirs, give the update up (or go back,
SKILL.md's step 5, when it already landed), and offer to update the workspace first and
try again after.
