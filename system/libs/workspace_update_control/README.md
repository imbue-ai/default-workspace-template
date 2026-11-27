# workspace-update-control

A workspace's updates as a client asks for them, and the dispatcher that arms the
workspace's own updates, starts a scheduled one and watches over a running one.
Standard library only: cron runs the dispatcher's tick with no venv, since a running
update may have broken it.

Everything here acts on the workspace whose root is the current directory, which is
where supervisord runs the shell and where the cron line changes to.

## Modules

- `control` -- one function per operation of the shell's update routes
  (`/api/workspace/update...`): start an update now, arm the schedule, refresh the
  client's values in an armed schedule, cancel it, skip a version, record a
  dismissal, read all of it, and read what tells whether a running update is alive.
  A client may name the values an update runs with (the newest version it knows, a
  version the user typed, the window in UTC, whether it holds backup credentials),
  and one it leaves out is the workspace's own: the version its release channel is
  on, the window of its update settings, whether it holds backup credentials
  itself. A schedule armed with the workspace's own values keeps them current.
  Starting answers at once: the create of the update's chat
  runs on in the background and `data/.state/update-apply/last-start.json` records
  how it went, naming the process that runs it, so an attempt whose process is gone
  reads as `CREATE_FAILED`.
- `dispatcher` -- the minute-by-minute tick: it dispatches an armed schedule inside
  its window, closes out or reminds a run with no verdict (and brings a stopped
  lead back), and holds the run's agents out of the memory guard's way.
- `settings` -- the update settings a client writes (the release channel, the feed
  its file is read from, whether the workspace arms its own updates, and the owner's
  local hours an update may start in), the conversion of those hours to the UTC
  window a schedule holds, in the timezone the workspace records, and whether the
  workspace holds backup credentials. Three settings are the workspace's own and win
  over the account's: its own answer on arming, and its own hours. A client that
  leaves one of them out keeps what is stored, and one that sends null clears it
  (`take_update_settings`).
- `channel` -- the version the release channel is on, read off the feed
  (`<feed>/<channel>-web.json`) at most once an hour and kept with how the last read
  went, and the newest version a client from before all this last named, which
  bounds the channel's for `CLIENT_CEILING_HOLD_SECONDS` after that client last
  asked.
- `arming` -- the workspace arming its own updates, each minute: the decision the
  Imbue Studio desktop's sweep made (`should_auto_arm`), taken from the workspace's
  own files.
- `installed_version` -- the version of the checkout, read from git, and how
  releases order. The shell's status route reads the version by the same rules.
- `release_tags` -- the fetch of the template's release tags into a checkout that
  names no version, which a workspace made from a published copy of the template
  is: it holds the template's history and none of its tags. The tags come from
  where the update-self skill fetches from, the `upstream` remote, or the address
  `system/config/parent.toml` names where the checkout has no such remote yet.
  Nothing is fetched into a checkout with no `parent.toml` (it was not made from
  the template) or into a shallow one, and an attempt is made a day after the last
  at the earliest. `arming` asks for it in the minute the channel's version is
  read, and never while an update is running.
- `tick` -- the tick as a program. `system/scripts/update_dispatcher_tick.py` is its
  entry file, which the `update-self-dispatch` cron line runs, and
  `system/scripts/install_update_dispatcher_cron.py` writes that line at boot.
- `agents` -- the workspace's agents read from the events file `mngr observe`
  writes, for the routes, which run no `mngr`. Where the file cannot vouch for the
  agents the answer is "unknown", never "none".
- `protocol` -- the state paths, the outcomes and skip reasons, and the text of the
  messages the dispatcher sends.

## Shared with the Imbue Studio desktop app

The files under `data/.state/update-apply/` (`schedule.json`, `last-dispatch.json`,
`skipped-target.json`, `dismissals.json`, `watchdog.json`, `dispatch-attempt.json`)
and the cron line `update-self-dispatch` are also written by the update program a
desktop from before the workspace's update routes ships into the workspace
(`apps/minds/imbue/minds/workspace_programs/` in the mngr repository). Whichever of
the two wrote the cron line last decides which dispatcher runs, so both must act
the same on the same files: the formats keep `UPDATE_PROGRAM_SCHEMA`, and a fix to
the dispatcher is made in both places, until no supported desktop ships that
program. This library's own reads-then-writes of the schedule, the update settings
and the skipped version take one lock (`state.lock`, `dispatcher.state_lock`), so a
cancel or a skip that lands while the tick or another request is writing is not
undone, and a schedule armed while the tick was deciding to arm one of its own, or
to cancel the one it had armed, stands as it was armed; that desktop's program takes none. The lock is held for file reads and
writes alone, never while a chat is created or the feed is read, and it is not the
lock cron's line tests. `last-start.json` is this library's alone, and so are the
files a workspace that arms its own updates keeps: `settings.json`,
`channel-version.json`
(which the update-self skill reads for its default target), `client-ceiling.json`,
`schedule-origin.json`, `installed-version.json` and `release-tags-fetch.json`. A
desktop from before them reads none, and `schedule.json` gains no field for them.

## A desktop from before the workspace armed its own updates

Such a desktop still sweeps the workspace every 300 seconds. Its arm, its start and
its refresh of an armed schedule name the newest version it knows (`ceiling`); a
sweep that finds nothing armed only reads, and names none. The routes serve those
requests as they always did and record the last one that named a version
(`client-ceiling.json`). For
`CLIENT_CEILING_HOLD_SECONDS` after it, the dispatcher arms nothing past that
version, a request that names none takes the older of the two, and an armed schedule
is left to that desktop's refreshes. A refresh that names a release the checkout is at
or past leaves the version of a schedule the workspace filled in itself as it is, and
rewrites the rest: written there, that release would start an update to what is
already installed. A workspace no such desktop is asking follows its channel.

A desktop from before the update routes asks no route: it runs the program it ships,
which points the cron line at that program's own dispatcher. That dispatcher reads no
channel and arms nothing by itself, so while the line names it the workspace follows
that desktop alone. The workspace's own line is written back when it boots, on an
arm, a start and a refresh of an armed schedule, and whenever a client writes the
update settings, which a current client does each time it opens the workspace.
