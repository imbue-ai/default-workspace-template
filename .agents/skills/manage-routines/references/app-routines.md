# Routines that belong to an app

What can and cannot be changed about a routine whose file is inside an app,
and how to tell which case a routine is in.

A routine whose file is inside an app (`system/apps/<package>/routines/`) is
part of that app: it goes wherever the app goes, and an app someone else
published brings its routines with it. It is listed, run, paused and watched
like any other, with the same commands. What can be changed differs, because
the file is the app's and the `routines` command never rewrites or deletes a
file inside an app (for an installed app, that would make it differ from
where it came from):

- **What the user can change is when it runs and whether it runs.** Turning
  it on or off (`routines enable` / `disable`) works as for any routine.
  Changing its schedule works too
  (`uv run --no-sync routines edit <id> --every 2h`, or Change schedule in the
  Routines app), but the new schedule is kept as the user's own choice in the
  routine's state (`data/.state/routines/<id>/state.json`), not in the file,
  and nothing is committed; `edit` prints the state file it went to. `list`
  and `show` print the schedule the routine actually runs on, so it can
  differ from what the file says.
- **Going back to the app's schedule** drops the user's own. In the Routines
  app the routine's page shows the app's schedule beside the user's and
  offers "Back to the app's schedule"
  (`DELETE /api/routines/<id>/schedule` on the Routines app). From the
  command line, edit the routine to exactly the schedule in its file, which
  does the same.
- **Everything else is a change to the app.** Editing any other field
  (`title`, `what`, the command or skill, the timeout) and removing the
  routine are refused, with a message naming the app's folder and the file.
  Make such a change by editing or deleting the routine's file inside the
  app's folder as part of changing that app (the `update-app` skill), and
  commit it with the app's other changes. For an app installed from someone
  else, that makes the app one this workspace has changed. To stop a routine
  running without changing the app, use `routines disable <id>`.

`uv run --no-sync routines list --format json` says which case a routine is
in, on each row: `owning_app` is the app whose folder holds the routine's
file (null for one under `system/routines/`), `is_schedule_only` is true when
the schedule and the switch are all that can be changed,
`has_schedule_override` is true while the user's schedule is in place of the
app's, and `app_schedule_text` is then the app's own schedule in words. The
Routines app shows the same: it names the app on the routine's page and
offers no Remove for it.
