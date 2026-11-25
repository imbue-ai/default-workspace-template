`update-self` takes its default target from the workspace itself: with no version named, `resolve-target` updates to the release the workspace's release channel is on, as the workspace's update dispatcher last read it (`data/.state/update-apply/channel-version.json`). It no longer asks an Imbue Studio desktop app for the app's version, which a workspace whose owner has no desktop running could never be answered.

- A pass with no pin in a workspace that has read no version for its channel is a fault that says why (no update settings yet, or the channel's file could not be read) and asks for a version to be named.

- A version the user names, and a pass that pins its ceiling in its first message, resolve as before. An override in a workspace with no channel's version has no ceiling to exceed.

- `manage-routines` says that the workspace schedules its own updates through the `update-self-dispatch` cron entry.
