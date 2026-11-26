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

## Standing instructions

- **Run every command from the repo root** (`/home/user/workspace`).
- **Keep git and file words out of what you say to the user**: they are
  getting a newer version of an app
  (`.agents/shared/references/user-facing-language.md`).
- **Never update without a way back.** A `no_snapshot` result is fixed, never
  worked around.
- **Never edit the app's program file or skill links.** They are generated
  from the app's `app.toml`, and the update regenerates them, keeping the
  app's port.
- **Done means the user can open the app and see their own data, as before.**
  A program that starts or a route that answers 200 is not done.

## What an update keeps

- **The workspace's own changes survive.** The new version is merged over
  them. A conflict in `uv.lock` alone is settled for you: the publisher's
  lock is taken and made again.
- **A schedule the user gave one of the app's routines is kept.** It lives in
  the routine's state, not in the app.

## Other tasks

- **Changing the app yourself** is the `update-app` skill, as for any app.
- **Removing it** is `uv run --no-sync app-install uninstall <name>`, which
  keeps its stored data and secret files.

## Steps

1. Find the app and what it is on.
2. See what is coming, and say so before starting. *Ends the turn* before a
   major version, until the user says go ahead.
3. Update. *Ends the turn* when a choice is the user's or an approval is
   awaited.
4. Check it against the user's own data.
5. Go back to the version before, only when the new version mishandles the
   user's data and the data cannot be brought forward.

## Step 1: Find the app and what it is on

```bash
uv run --no-sync app-install list
```

Find the app in `apps`. Its `state` says what it is to this workspace:

- `installed` or `modified` -- it came from `upstream_url`; carry on.
  `modified` means this workspace has changed it, and the update keeps those
  changes.
- `owned` -- this workspace built it, so there is nothing to update it from.
  A change to it is the `update-app` skill's.

Note three values before anything changes; the way back in step 5 needs them:
`upstream_version` (the version it is on, empty for an app that never had
one), `upstream_commit`, and `upstream_url`.

## Step 2: See what is coming, and say so before starting

The update takes the repository's highest `v<major>.<minor>.<patch>` tag, or
its `main` branch when it has no such tag, unless you name a tag or branch
with `--branch`. List the tags to see which version that is:

```bash
git ls-remote --tags --refs -- <upstream_url>
```

A listing that is refused means the repository is private to someone: request
access as under `needs_github_access` in step 3, and skip this check.

Tell the user, in one or two plain sentences, which version they are on and
which one is coming, and that it is new code from the same source they chose
to trust when they added the app, which Imbue has not reviewed.

**When the new version's first number is higher than the installed one**
(2.0.0 over 1.4.2), it is a major version: one the publisher expects people
to have to act on. Say that before you start, say that their data is copied
aside first so the update can be undone, then end the turn and wait for their
go-ahead. For any other version, carry on unless the user only asked what is
available.

An app whose `upstream_version` is empty (its manifest names no version) has
nothing to compare: an update is there whenever the repository's newest
commit differs from `upstream_commit`.

## Step 3: Update

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
  `snapshot` is where the app's data was copied before the update ("The copy
  of the data" in `references/the-apps-data.md`). `problems` lists what could not be done once the
  new version was in: something its environment declares could not be
  installed, or supervisord did not restart its programs. Go to step 4.
- `conflict` -- the new version changes the same lines this workspace
  changed. `conflicted_paths` names the files, and `worktree` is where the
  half-merged app waits (`data/.state/app_install/worktrees/<name>`). Follow
  `references/when-the-update-waits.md`.
- `tests_failed` -- the new version merged, but its environment did not sync
  from its lock or its own tests fail here; `detail` carries the output. The
  merged app waits in `worktree`. Follow
  `references/when-the-update-waits.md`, reading its "A version made for a
  newer workspace" first.
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
  update up ("Giving an update up" in `references/when-the-update-waits.md`),
  settle the clash, and start again.
- `needs_github_access` -- the repository is private to someone. Request the
  GitHub read permission yourself, as any other latchkey permission request
  (the `connect-external-service` skill has the command; scope `github-git`,
  permission `github-git-read`), end the turn, and run the update again once
  it is granted.
- `unreachable` -- the repository could not be read for a reason access
  would not change (it is gone, the tag does not exist, the network is down).
  Say what went wrong in plain words, from `detail`.
- `refused` -- nothing changed; `detail` says why. The cases: the app was
  built here; an earlier update of it is still waiting (finish it or give it
  up); `--continue` was passed with nothing waiting; this git has no
  `git subtree`; or the finished update no longer fits the workspace as it is
  now. `references/when-the-update-waits.md` covers the waiting cases.

## Step 4: Check it against the user's own data

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

When the new version does not read the old data as it is, read
`references/the-apps-data.md` before you touch the data: it has how to bring
the data forward, and where the copy the update made is.

## Step 5: Go back to the version before

When the new version mishandles the user's data and bringing the data forward
is not possible or not wanted, put the data back and reinstall the version
the app was on, in that order: follow "Going back to the version before" in
`references/the-apps-data.md`. Then check the app as in step 4, and tell the
user they are back on the version they had, what went wrong with the new one,
and that you can try again when a later version comes out.
