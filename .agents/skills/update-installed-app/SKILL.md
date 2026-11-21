---
name: update-installed-app
description: Bring a newer version of an app this workspace installed from someone else's repository in over the workspace's own changes to it, then check it against the user's data. Use when the user asks to update, upgrade, or get the latest version of an app they added (not one built here), or asks whether one has a newer version. For a whole adopted workspace (a template), use update-installed-template; for the workspace itself, update-self.
metadata:
  author: imbue
---

# Updating an installed app

An installed app is one this workspace added from someone else's repository
(the `install-app` skill). It stays connected to where it came from, so a
newer version can be brought in on top of whatever this workspace has changed
in it. The app's stored data is not part of the app's code: it stays under
`data/.apps/<name>/` across every update, so a new version meets data the old
version wrote.

All commands run from the repo root (`/home/user/workspace`). Keep git and
file words out of what you say to the user: they are getting a newer version
of an app (`.agents/shared/references/user-facing-language.md`).

## 0. Find the app and what it is on

```bash
uv run --no-sync app-install list
```

Find the app in `apps`. Its `state` says what it is to this workspace:

- `installed` or `modified` -- it came from `upstream_url`; carry on.
  `modified` means this workspace has changed it, and the update keeps those
  changes.
- `owned` -- this workspace built it, so there is nothing to update it from.
  A change to it is the `update-app` skill's.

Note three values before anything changes; the way back in step 4 needs them:
`upstream_version` (the version it is on, empty for an app that never had
one), `upstream_commit`, and `upstream_url`.

## 1. See what is coming, and say so before starting

The update takes the repository's highest `v<major>.<minor>.<patch>` tag, or
its `main` branch when it has no such tag, unless you name a tag or branch
with `--branch`. List the tags to see which version that is:

```bash
git ls-remote --tags --refs -- <upstream_url>
```

A listing that is refused means the repository is private to someone: request
access as under `needs_github_access` in step 2, and skip this check.

Tell the user, in one or two plain sentences, which version they are on and
which one is coming, and that it is new code from the same source they chose
to trust when they added the app, which Imbue has not reviewed.

**When the new version's first number is higher than the installed one**
(2.0.0 over 1.4.2), it is a major version: one the publisher expects people
to have to act on. Say that before you start, say that their data is copied
aside first so the update can be undone, then end the turn and wait for their
go-ahead. For any other version, carry on unless the user only asked what is
available.

An app with no version (it was installed from a branch) has nothing to
compare: an update is there whenever the repository's newest commit differs
from `upstream_commit`.

## 2. Update

```bash
uv run --no-sync app-install update <name>                  # the latest version
uv run --no-sync app-install update <name> --branch <ref>   # a particular tag or branch
```

The command asks nothing and ends in one JSON line. The app the user is
running is untouched until the result is `updated`: the new version is merged,
registered and checked in a place of its own first. Act on its `result`:

- `up_to_date` -- the app is already at that version. Say so.
- `updated` -- the new version is live and its programs were restarted.
  `previous_version` and `version` are the versions it went from and to.
  `snapshot` is where the app's data was copied before the update (see "The
  copy of the data" below). `problems` lists what could not be done once the
  new version was in: something its environment declares could not be
  installed, or supervisord did not restart its programs. Go to step 3.
- `conflict` -- the new version changes the same lines this workspace
  changed. `conflicted_paths` names the files, and `worktree` is where the
  half-merged app waits (`data/.state/app_install/worktrees/<name>`). Follow
  "When the update waits" below.
- `tests_failed` -- the new version merged, but its environment did not sync
  from its lock or its own tests fail here; `detail` carries the output. The
  merged app waits in `worktree`. Follow "When the update waits" below, and
  read "A version made for a newer workspace" first.
- `no_snapshot` -- the app's data could not be kept safe, so the update did
  not start. `detail` says which case it is. The data does not fit on the
  disk beside itself and the workspace has no recent backup: free space, or
  get a backup made (`uv run --no-sync host-backup-now`, and
  `uv run --no-sync host-backup-now --check` says whether one is recent
  enough), then run the update again. Or the copy failed part-way, usually
  because the running app changed a file meanwhile: run the update again.
  Never work around this by updating without a way back.
- `collision` -- the new version would take a name something in this
  workspace already holds (a skill, a routine, a program); `problems` names
  each. The app is unchanged, and the merged version waits in its worktree.
  Tell the user what clashes and ask which one should give way. Then give the
  update up (below), settle the clash, and start again.
- `needs_github_access` -- the repository is private to someone. Request the
  GitHub read permission yourself, as any other latchkey permission request
  (`.agents/skills/connect-external-service/references/latchkey.md`, scope
  `github-git`, permission `github-git-read`), end the turn, and run the
  update again once it is granted.
- `unreachable` -- the repository could not be read for a reason access
  would not change (it is gone, the tag does not exist, the network is down).
  Say what went wrong in plain words, from `detail`.
- `refused` -- nothing changed; `detail` says why. The cases: the app was
  built here; an earlier update of it is still waiting (finish it or give it
  up, below); `--continue` was passed with nothing waiting; this git has no
  `git subtree`; or the finished update no longer fits the workspace as it is
  now (see "When the update waits").

### When the update waits

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

It answers with the same results as step 2. `conflict` again, with a
`detail`, means the worktree is not ready: a resolution that is not committed
yet, or a merge that was taken back there (`git merge --abort`), which leaves
no new version to finish; give that one up and start again. `refused` with a
`detail` saying the result does not merge into the primary checkout means the
workspace changed the same lines while the update waited: merge the
workspace's branch into the worktree
(`git -C data/.state/app_install/worktrees/<name> merge "$(git branch --show-current)"`),
commit, and run `--continue` again.

### Giving an update up

```bash
git worktree remove --force data/.state/app_install/worktrees/<name>
```

The app the user runs was never touched, and the next `update` starts
afresh. Do this when the user decides against the new version, when a
conflict or a failing check is not worth settling, and before starting over
after a `collision`.

### A version made for a newer workspace

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
step 4, when it already landed), and offer to update the workspace first and
try again after.

## 3. Check it against the user's own data

**Done means the user can open the app and see their own data, as before.** A
program that starts or a route that answers 200 is not done.

1. Check the app came up and shows the user's real content, and reload any
   window of it the user has open:

   ```bash
   uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --marker "<text the user's own data should show>" --screenshot /tmp/<name>-updated.png --refresh
   ```

   Look at the screenshot. When the app did not come up, read its log (the
   `check-app-errors` skill) before anything else.
2. Read what the new version asks for that the old one did not. The update's
   result does not list it, so read `system/apps/<package>/app.toml`:
   - each `[[permissions]]` entry not yet granted: file the request yourself,
     each in a tool call of its own;
   - each `[[secrets]]` entry whose file `data/.secrets/<file>.env` does not
     exist: request it with the `connect-external-service` skill's
     `request_secret.py`. A program that runs under a file that does not
     exist yet cannot start, so the app stays down until it arrives;
   - `[llm]`, when it is new: the `use-ai-integration` skill;
   - `[[dependencies]]`: tell the user which apps it expects that are not
     here. Nothing installs them;
   - a new script under `system/apps/<package>/env.d/`: env-converge runs it
     at the next boot. Run it by hand when the app needs what it installs now
     (`bash system/apps/<package>/env.d/<file>`).
3. Take each entry of `problems` in turn. An apt package that does not exist
   at this workspace's pinned snapshot can be reached with
   `uv run --no-sync env-converge upgrade`; offer that.
4. Use the app the way the user does, on their data: open what they had, and
   check that the things the new version changed still read what the old
   version wrote. Records missing, counts that dropped, a page that errors on
   older entries, dates or names shown wrong: each means the new version does
   not read the old data as it is.

### When the data needs bringing forward

A well-made app converts its own older data when it starts. When this one did
not, and the new version needs the data in a new shape, converting it is
yours to do, and the copy the update made is the way back:

1. Say what you found and what you are about to do, in the user's terms, and
   that you can put everything back as it was.
2. Check the copy exists: `snapshot` in the update's result names it. When
   `snapshot` is empty and the app has stored data, there is no copy (see
   below), so make one before touching anything, as the `update-app` skill's
   "Protect the user's data while you verify" says.
3. Stop the app's programs (`supervisorctl stop <name>`, and each
   `<name>-<role>`), convert the data under `data/.apps/<name>/`, start the
   app again (`verify_app.py <name> --restart ...`), and repeat the check in
   point 4 above.
4. When it holds, tell the user what changed. When it does not, go back.

### The copy of the data

Before an update the command copies `data/.apps/<name>/` to
`data/.state/app_install/snapshots/<name>/<the first characters of the
commit the app was on>/`, outside the backup. Only the newest copy of an app
is kept: the next update of the same app replaces it. `snapshot` is empty in
two cases: the app had no stored data, or the data did not fit on the disk
beside itself and the update went ahead because the workspace has a recent
backup. In the second case the backup is the way back
(`system/services/host_backup/README.md`), and the user should hear that
before you change their data.

## 4. Going back to the version before

When the new version mishandles the user's data and bringing the data
forward is not possible or not wanted, put the data back and reinstall the
version the app was on. **Put the data back first**: the reinstall is itself
an update, so it takes a new copy of whatever is in `data/.apps/<name>/` and
keeps only that one.

1. Stop the app's programs: `supervisorctl stop <name>`, and each
   `<name>-<role>`.
2. Set the current data aside and put the copy in its place:

   ```bash
   uv run --no-sync disk-headroom /var/tmp          # the move must leave room to spare
   mv data/.apps/<name> /var/tmp/<name>-data-after-update
   cp -a <snapshot> data/.apps/<name>
   ```

   Anything the user added through the app since the update is only in the
   set-aside copy. Say so, and ask before deleting it.
3. Reinstall the earlier version, by the values noted in step 0:

   ```bash
   uv run --no-sync app-install update <name> --branch v<the earlier upstream_version>
   ```

   For an app that had no version, pass the earlier `upstream_commit` in full
   as the `--branch` value. The results are step 2's; `updated` here means
   the app is back on the earlier version, with this workspace's own changes
   to it still in place, and its programs started again.
4. Check it as in step 3, then tell the user they are back on the version
   they had, what went wrong with the new one, and that you can try again
   when a later version comes out.

## Things to know

- **The workspace's own changes survive.** The new version is merged over
  them. A conflict in `uv.lock` alone is settled for you: the publisher's
  lock is taken and made again.
- **Never edit the app's program file or skill links.** They are generated
  from the app's `app.toml`, and the update regenerates them, keeping the
  app's port.
- **A schedule the user gave one of the app's routines is kept.** It lives in
  the routine's state, not in the app.
- **Changing the app yourself** is the `update-app` skill, as for any app.
  **Removing it** is `uv run --no-sync app-install uninstall <name>`, which
  keeps its stored data and secret files.
