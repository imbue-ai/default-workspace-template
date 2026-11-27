The update dispatcher arms a workspace's own updates, with no client present.

- `settings` holds the update settings a client writes (`data/.state/update-apply/settings.json`): the release channel, the feed its file is read from, whether the workspace arms its own updates, and the owner's local hours an update may start in, which are read in the timezone the workspace records and in UTC when it records none. Three are the workspace's own and win over the account's: its own answer on arming, and its own hours (`window_start_hour_here` and `window_end_hour_here`, both set or both null). `take_update_settings` keeps the stored value of each of those three that a client leaves out and clears one sent as null, so a client that writes the account's settings cannot wipe what the owner set in another client.

- `channel` reads the version the channel is on from `<feed>/<channel>-web.json`, at most once an hour and within a hard timeout, and keeps the last answer with how the last read went (`channel-version.json`). A read that fails keeps the last version for display.

- `arming` is the desktop sweep's `should_auto_arm` taken from the workspace's own files. Each minute, when arming is on, nothing is armed or running and no decision is waiting, it arms a schedule for the channel's version if the workspace is behind it, unless the user skipped that version or a run already went out for it. A channel file that cannot be read arms nothing. A workspace behind by a patch alone is armed too.

- `control` takes `ceiling`, the window and the backup flag as optional. A value left out is the workspace's own, and a schedule armed with such values keeps them current. A request that names a `ceiling` comes from a desktop built before this: its version is recorded (`client-ceiling.json`), and for 15 minutes after it the workspace arms nothing past it and leaves an armed schedule to that desktop's refreshes.

- `installed_version` reads the checkout's version from git by the rules the shell's status route uses, which now come from here.

- `tick` runs the arming after the dispatcher's minute, and `install_cron_line_main` writes the dispatcher's cron line for the boot.

- The library's own reads-then-writes of the schedule, the update settings and the skipped version take one lock (`data/.state/update-apply/state.lock`), held for file reads and writes alone. A cancel or a skip that lands while the dispatcher or another request is writing the schedule is not undone, two clients writing the settings at once lose no setting of the workspace's own, and a version skipped while the dispatcher was arming it is not left armed.

No file a released desktop reads or writes changes format.
