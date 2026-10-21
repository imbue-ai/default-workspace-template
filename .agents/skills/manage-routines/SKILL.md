---
name: manage-routines
description: See, add, change, run, pause, or remove the routines of this workspace -- everything that runs on a schedule (a command, or a skill run in a chat of its own). Use when the user asks for anything periodic ("every morning", "once a week", "every 15 minutes", "remind me at 3pm"), asks what runs on its own, or wants a scheduled thing turned off, retimed, run now, or fixed. Also covers the built-in weekly Caretaker, which is a routine like any other.
metadata:
  author: imbue
---

# Managing routines

A **routine** is something that runs on a schedule and then exits: a command
run from the repo root, or a skill run in a chat of its own (what the
workspace calls an automation). Each is one TOML file under
`system/routines/<id>.toml` (the root `routines/` folder points there),
tracked like an app or a skill. The `routines` command is the only way one is
created or changed; the Routines app (`http://routines.<workspace-host>/`) is
where the user sees them. Nothing is registered anywhere else: no cron lines,
no supervisord programs, no extra template.

Services that run continuously are not routines; they stay under supervisord
(the `update-app` skill). An app that needs periodic work makes it a routine
with `--app <name>`, never a loop or a thread inside its service.

## First: confirm the timezone

Schedules are in the container's local time, which the bootstrap sets from the
user's machine at each boot. The user may have moved since, so before writing
a schedule check it:

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/timezone   # -> {"timezone": "America/Los_Angeles"}
cat /etc/timezone
```

If they differ, update the container before scheduling anything:

```bash
ln -sf "/usr/share/zoneinfo/<Area/City>" /etc/localtime
echo "<Area/City>" > /etc/timezone
echo "<Area/City>" > /home/user/workspace/data/.state/user_timezone
```

Routines read the clock on every tick, so the change applies at once.

## Add a routine

A command, every day at 7:30 am:

```bash
uv run --no-sync routines add news-sync --title "News sync" --what "Pulls the morning headlines into a note." \
    --every 1d --at 07:30 -- uv run --no-sync python scripts/sync_news.py
```

A skill run in a chat of its own (an automation), on weekday mornings:

```bash
uv run --no-sync routines add inbox-digest --title "Morning inbox digest" --what "Reads new mail and writes a short summary note." \
    --every 1d --at 08:00 --days mon,tue,wed,thu,fri --skill inbox-digest
```

A one-off, once at a moment (local time):

```bash
uv run --no-sync routines add dentist-reminder --title "Dentist reminder" --what "Reminds you about Thursday's appointment." \
    --once 2026-11-05T09:00 --skill dentist-reminder
```

The schedule is `--every` (a number and `m`, `h`, `d`, `w` or `mo`; a minute
or longer) with an optional `--at HH:MM` (daily or slower), `--days` (weekdays,
comma separated) and `--day-of-month` (monthly), or `--once YYYY-MM-DDTHH:MM`.
Other flags: `--app <name>` (the app it belongs to), `--timeout` (how long a
run may take; `1h`), `--retry-after` (the gap before a failed run is retried;
`2m`), `--notify-after-failures` (failures in a row before the user is told;
`3`), `--disabled` (start switched off). The id is the file's name: lowercase
letters, digits and hyphens.

The title and the sentence are the user's words for it, not the command's:
"Morning inbox digest" and "Reads new mail and writes a short summary note.",
never "run_sync.py" and "runs the sync".

A command that needs a credential the user supplied through the
`connect-external-service` skill's secret card runs under
`python3 system/scripts/with_secrets.py data/.secrets/<name>.env -- <command...>`
(after the `--`), which puts the file's variables in that process's
environment; never read or source the file in the command itself.

A new `--every` routine that is switched on is due at once (so the user sees it work), then on its schedule; a `--once` routine is due at its moment, and one added `--disabled` is not due until it is turned on.
To make it wait a full cadence first, run it now and let that count:
`uv run --no-sync routines run <id> --wait`.

The write is committed for you, by the routines library, staging only that
file. "Couldn't save that change because something else is saving right now"
means another writer held the index; try again in a moment.

## What a skill routine needs

The skill at `.agents/skills/<name>/SKILL.md`, written for a chat that starts
empty each run: the runner creates the routine's chat on its first run (from
the `automation` create template, with the workspace's default provider
account), and on later runs sends `/clear` and then `/<name>`. The run ends
when the agent's turn ends, and the first line of the agent's last message is
the run's summary in the list, so end the skill with one plain sentence saying
what was done. An agent that asks the user a question marks the run "Needs
you" and tells the user through the Imbue Studio feed; past the timeout the
turn is interrupted.

## See, change, run, pause, remove

```bash
uv run --no-sync routines list                      # every routine: health, schedule, next run, last run
uv run --no-sync routines show <id>                 # one routine with its recent runs
uv run --no-sync routines runs <id>                 # its recent runs
uv run --no-sync routines log <id> [<run id>]       # a run's output, the latest by default
uv run --no-sync routines edit <id> --every 2h      # any of the add flags; the rest stays
uv run --no-sync routines edit <id> --every 2h --at ""   # an empty --at, --days or --app clears the field (a kept 7:30 refuses a 2h cadence)
uv run --no-sync routines run <id> --wait           # run it now and report the outcome
uv run --no-sync routines disable <id>              # pause: nothing runs until it is enabled
uv run --no-sync routines enable <id>
uv run --no-sync routines remove <id>               # delete it (refused while a run is going); its chat stays in the chat list
```

`--format json` on `list`, `show` and `runs` is for scripts. A routine's
health is one word: Working, Failing (its last runs failed; the runner retries
after `retry_after`), Overdue (it should have run and did not, which means the
tick is not running), Needs you (its agent waits on the user), Broken (its
file does not validate; `show <id>`, or `list --format json`, says why), Not
run yet, Done (a one-off that ran), or Off.

## How it runs

One cron line, written at every boot, runs `routines tick` once a minute. The
tick starts every routine that is due, in a process of its own, and closes off
runs whose process is gone. A run holds the routine's lock for its whole
length, so a second run (scheduled or manual) is refused while one is going.
Missed windows catch up once when the workspace is back, never replayed. Every
run's output is streamed to `data/.state/routines/<id>/runs/<run id>/log`, and
the run ledger is `data/.state/routines/events/runs/events.jsonl`. After
`notify_after_failures` failures in a row the user is told once through the
Imbue Studio feed, and once more when the routine works again.

A command may write `$ROUTINES_RUN_DIR/result.json` with a one-line
`summary` (shown in the list) and `links` for the run page.

Full reference: `system/libs/routines/README.md`.

## The Caretaker

The built-in weekly check-up is the routine `caretaker`
(`system/routines/caretaker.toml`), off by default. It is turned on from the
Routines app or with `uv run --no-sync routines enable caretaker`, and off
again with `disable`; retime it with `routines edit caretaker --every 1d`.
Its check (`system/services/caretaker/caretaker_check.sh`) looks at the
workspace's services, logs, disk, memory guard and the other routines, and
wakes the Caretaker's chat only with findings (or once, to introduce itself).
Never enable it without the user asking; it is a beta feature.
