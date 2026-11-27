- The `devices` and `file-sharing` skills say what to do when no desktop is connected to a workspace, which is permanent for an owner who uses Imbue Studio in a browser alone: tell the user once that the feature needs the Imbue Studio desktop app, and do not try it again in that conversation.

- `update-self` takes its default target from the workspace itself: with no version named, `resolve-target` updates to the release the workspace's release channel is on, as the workspace's update dispatcher last read it (`data/.state/update-apply/channel-version.json`). It no longer asks an Imbue Studio desktop app for the app's version, which a workspace whose owner has no desktop running could never be answered. The one exception is a workspace that holds no update settings, which only a desktop app from before those settings has opened: it still asks that app.

- A pass with no pin in a workspace that has read no version for its channel is a fault that says why (no update settings yet, or the channel's file could not be read) and asks for a version to be named.

- A version the user names, and a pass that pins its ceiling in its first message, resolve as before. An override in a workspace with no channel's version has no ceiling to exceed.

- `manage-routines` says that the workspace schedules its own updates through the `update-self-dispatch` cron entry.

- `migrate-workspace` leaves behind everything the old workspace's backup setup kept: the copies of `restic.env` it kept aside, the credentials of a setup it had not finished, and the record of that setup. This workspace's own backup setup would otherwise install the old workspace's repository as its own. It also leaves behind what the old workspace recorded of its own update runs (the run and its verdict, the armed schedule, the last dispatch and start), which this workspace's dispatcher would act on as its own; the update settings and the skipped version come over.

- `build-app` refuses `backup-setup` and `backup_setup` as an app's name, which the workspace's own backup setup runs under.
