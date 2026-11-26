# The app's data across an update

The app's stored data (`data/.apps/<name>/`) outlives every version of its
code. This file has the copy an update makes of it, how to bring older data
forward when the new version does not, and how to go back to the version
before.

## Contents

- The copy of the data
- When the data needs bringing forward
- Going back to the version before

## The copy of the data

Before an update the command copies `data/.apps/<name>/` to
`data/.state/app_install/snapshots/<name>/<the first characters of the
commit the app was on>/`, outside the backup. Only the newest copy of an app
is kept: the next update of the same app replaces it. `snapshot` is empty in
two cases: the app had no stored data, or the data did not fit on the disk
beside itself and the update went ahead because the workspace has a recent
backup. In the second case the backup is the way back
(`system/services/host_backup/README.md`), and the user should hear that
before you change their data.

## When the data needs bringing forward

A well-made app converts its own older data when it starts. When this one did
not, and the new version needs the data in a new shape, converting it is
yours to do, and the copy the update made is the way back:

1. Say what you found and what you are about to do, in the user's terms, and
   that you can put everything back as it was.
2. Check the copy exists: `snapshot` in the update's result names it. When
   `snapshot` is empty and the app has stored data, there is no copy ("The
   copy of the data" above), so make one before touching anything, the way
   the `update-app` skill snapshots an app's store before a change that
   rewrites it.
3. Stop the app's programs (`supervisorctl stop <name>`, and each
   `<name>-<role>`), convert the data under `data/.apps/<name>/`, start the
   app again (`verify_app.py <name> --restart ...`), and use the app on the
   user's data again, as SKILL.md's step 4 does.
4. When it holds, tell the user what changed. When it does not, go back.

## Going back to the version before

When the new version mishandles the user's data and bringing the data
forward is not possible or not wanted, put the data back and reinstall the
version the app was on. **Put the data back first**: the reinstall is itself
an update, so it takes a new copy of whatever is in `data/.apps/<name>/` and
keeps only that one.

Two values are needed: the commit the app was on, and the copy of the data.
When the update ran in an earlier chat and neither was noted, both are still
in the workspace. The copy is the one directory under
`data/.state/app_install/snapshots/<name>/` (none there means the update made
none: see "The copy of the data" above). The commit is in the record's own
history: `git log -p -- system/config/apps/<name>.toml` shows each change to
the record, and the newest one that changes `upstream_commit` removed the
commit the app was on, in full.

1. Stop the app's programs: `supervisorctl stop <name>`, and each
   `<name>-<role>`.
2. Set the current data aside and put the copy in its place (skip this for an
   app that had no stored data; when the copy was not made because the data
   did not fit, bring the data back from the backup instead, as
   `system/services/host_backup/README.md` describes):

   ```bash
   uv run --no-sync disk-headroom /var/tmp          # the move must leave room to spare
   mv data/.apps/<name> /var/tmp/<name>-data-after-update
   cp -a <snapshot> data/.apps/<name>
   ```

   Anything the user added through the app since the update is only in the
   set-aside copy. Say so, and ask before deleting it.
3. Reinstall the earlier version, by the `upstream_commit` noted before the
   update (SKILL.md, step 1), in full:

   ```bash
   uv run --no-sync app-install update <name> --branch <the earlier upstream_commit>
   ```

   The commit works wherever the app came from. A tag `v<upstream_version>`
   exists only in a published repository, so it is not there for an app
   taken from someone's dev repository or from a branch. The results are
   the update's own (SKILL.md, step 3); `updated` here means the app is back
   on the earlier version, with this workspace's own changes to it still in
   place, and its programs started again.
4. Go back to SKILL.md's step 5 for the check and what to tell the user.
