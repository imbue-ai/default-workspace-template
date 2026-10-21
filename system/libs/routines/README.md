# routines

Everything in the workspace that runs on a schedule: the definitions, the
scheduler, the runner, the run ledger, and the `routines` command. The design
is `docs/system/specs/routines/spec.md`; this README is the reference for the
files and the command.

A **routine** is one TOML file under `system/routines/<id>.toml` (tracked in
git like an app or a skill; the root `routines` symlink points at the
directory) plus its state under `data/.state/routines/<id>/`. The id is
lowercase letters, digits and hyphens, and never `events`, which is the run
ledger's directory beside the routines' own. It runs a **command** from the
repo root, or a **skill** in a chat of its own.

```toml
title = "Morning inbox digest"
what = "Reads new mail and writes a short summary note."
every = "1d"
at = "07:30"
skill = "inbox-digest"
```

| Field | Meaning |
| --- | --- |
| `title`, `what` | What the user sees: a name and one sentence. |
| `every` | The cadence: a number and `m`, `h`, `d`, `w` or `mo`. |
| `at` | The local time of day a run is due (`HH:MM`), with a cadence of a day or more. |
| `days` | Weekdays a run may start: `["mon", "fri"]`. |
| `day_of_month` | The day a monthly routine is due. |
| `once` | A single local moment (`2026-11-03T15:00`) instead of `every`; done after one success. |
| `command` | The argv to run, from the repo root, with the agent environment. |
| `skill` | The skill to run in the routine's chat (`/<skill>` after a `/clear`). |
| `enabled` | The default switch (`true`); the user's choice lives in the state. |
| `timeout` | How long a run may take (`1h`). |
| `retry_after` | How long after a failed run the next attempt may start (`2m`). |
| `notify_after_failures` | Failures in a row before the Imbue Studio feed is told (`3`; `0` never). |
| `app` | The app the routine belongs to, for grouping. |

## How it runs

One cron line, written by the bootstrap at every boot, runs `routines tick`
every minute through `with_agent_env.sh`. The tick closes off runs whose
process is gone, spawns `routines run <id> --trigger scheduled` for every
routine that is due, and once a day archives the ledger past 16 MB and prunes
run directories older than 90 days.

A run holds the routine's lock for its whole length (a second run, scheduled
or manual, is refused once a few tries over half a second have found it held),
records itself in the state, writes `run_started` to the ledger, streams the
command's output to `data/.state/routines/<id>/runs/<run id>/log`, and writes
`run_finished` with the outcome (`SUCCEEDED`, `FAILED`, `TIMED_OUT` or
`INTERRUPTED`). A command may leave `$ROUTINES_RUN_DIR/result.json` with a
one-line `summary`, `links` for the run page, the `chat_id` of a chat it
used, an `outcome` that says how a failed run ended better than its exit code
(`TIMED_OUT`; a non-zero exit is never a success), and `warnings`
(`routines wake` writes all of these, so a run that wakes a chat keeps the
chat, a timed-out wake is a timed-out run, and a restarted chat shows as the
run's warning).

A skill run asks the chat app for the routine's chat (creating it from the
`automation` template on the first run), restarts it if the previous run is
still going (and says so as a warning on the new run), sends `/clear` and
`/<skill>`, and waits until the chat is idle with an agent reply, which
becomes the run's summary. A chat waiting on the user marks the run blocked
and tells the feed; past the timeout the chat is interrupted.

The due moment is the last success plus the cadence, moved to `at` on that day
(the day the successful run started, so a run that crosses midnight claims no
second day) and to the next listed weekday; a never-run routine is due now; a
failed attempt is retried no sooner than `retry_after` after it started; missed
windows catch up once and are never replayed. The state file keeps
`last_started_at`, `last_succeeded_at`, `last_success_started_at`,
`consecutive_failures`, the run in flight and the last run; delete
`last_succeeded_at` to make a routine due.

After a run the runner tells the feed once per streak when
`notify_after_failures` is reached, once when the routine works again, and once
per run when its agent waits on the user; a notification lands on the
routine's page in the Routines app.

## The command

```bash
routines list [--format json]
routines show <id> [--format json]
routines add <id> --title "..." --what "..." (--every 1d [--at 07:30] [--days mon,fri] [--day-of-month 1] | --once 2026-11-03T15:00) (--skill name | -- <command...>) [--app name] [--timeout 1h] [--retry-after 2m] [--notify-after-failures 3] [--disabled]
routines edit <id> [the same flags]
routines enable <id> / routines disable <id>
routines run <id> [--wait]
routines runs <id> [--limit N] [--format json]
routines log <id> [<run id>]
routines remove <id>
routines wake <skill> [--routine <id>] [--title "..."] [--timeout 1h]
routines tick
routines migrate-cron
```

Every definition write is committed by the library, staging that one file; a
commit refused because another writer holds the index is retried once and then
reported as "Couldn't save that change because something else is saving right
now. Try again in a moment.", with the file left written.

`routines migrate-cron` converts the `data/.state/cron.d` entries a workspace
wrote before routines existed, carries the old runner's state over, and
removes both copies of each converted entry; the bootstrap runs it at every
boot.
