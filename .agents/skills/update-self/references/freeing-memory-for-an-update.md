# Freeing memory so the update can finish

An update's heaviest moments -- launching the worker, its test gate, the frontend build -- are the
ones the memory guard sheds first. When that happens, this pass frees memory itself rather than
stopping to ask, within the licence below. It is the one flow that may: every other one offers the
candidates to the user and stops only what they approve
(`.agents/shared/references/freeing-memory.md`).

## Read the memory the guard acts on, not the container's

```bash
python3 system/services/oom_priority/bin/memory_candidates.py
```

Its first line is the free memory **earlyoom decides on**: the headroom file the workspace
publishes for `--host-meminfo` while it is fresh, else `/proc/meminfo`, and it names which it read.
Under a memory cap those two disagree, and the container's own `/proc/meminfo` is the optimistic
one -- it has read three times the real headroom on a capped workspace. `free -m` reads that
optimistic file, so do not decide anything from it, and never tell the worker to rerun on a figure
that came from it: the gate will be shed again and the round is wasted.

The command lists the idle chats and workers, the apps the workspace may stop with how many windows
show each, and the browsers no window shows, each with its memory. The figures are summed RSS, so
they rank candidates rather than promise what stopping one frees. Read the free memory again after
each round.

## What this pass may stop

**Every run** may stop what the Step 1 licence names: chats and background agents, the browser, and
the user apps no window is showing.

**A scheduled run** may stop **any** app the candidates command's apps section lists, windows open
or not. That section is the boundary, and it is drawn so that everything in it can be brought back:
it holds no critical app (the shell, the chat and the terminal are what the workspace is reached and
talked to through), nothing sharing a critical app's program, nothing internal, and nothing without
a supervisord program to start again.
Nothing outside it is yours to stop -- not a service, not the shell, not the user's data.

Tell the two apart by who dispatched the run (before the Step 2a handoff, run the workspace's own
`.agents/skills/update-self/scripts/update_self.py` instead of the staged copy):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status started-by
```

`scheduled` is the broader licence. `immediate` and `unknown` are the narrow one, which is also
where a pressed "Update now" lands: the user is at the screen and an app going dark under them is a
surprise, so a windowed app is still theirs to approve.

Stop as little as the run needs. Take the candidates largest first, re-read the free memory, and
stop when there is room -- a scheduled licence is not a reason to clear the workspace.

An app whose `AUTO-STOP` column reads `no` (`stop_when_no_windows: false` in `--json`) may be one
that does work between requests (a poller, a scheduled refresh, a job that outlives its window; the
`build-app` skill sets the field that way on purpose). It is still yours to stop, but prefer the
others first, and say in the results message that it was down for the update.

## Stop the program, never the windows

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status stop-apps <program> <program> ...
```

The programs are the `PROGRAM` column of the candidates listing. Do **not** quit an app through the
shell, which closes its windows.

A stopped program leaves its windows where they are. The shell parks the app's port and answers that
window "Starting <app>" if its page reloads -- and that connection wakes the app again
(`system_interface/shell/app_lifecycle.py`). So an app someone is actually looking at comes straight
back, which is the right behaviour and also why the headroom has to be read again rather than
assumed. On an unattended run nothing reloads and the app stays down.

If a window did get closed -- by this pass, or by an app that tore its own down -- open it again at
the end (`uv run --no-sync workspace-layout open <app>`), because nothing else will.

## Putting it back

Run this before you compose the results message, whatever the outcome:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-apps
```

It starts every app the record names and drops each one as it comes up, so it is safe to re-run and
a no-op when nothing was stopped or a landed apply got there first. An app it cannot start stays on
the record, and the next run carries it over, unless the update removed its program.

A browser is not a program, so no ending brings one back: the browser service restores a stopped
browser as stopped, with its tabs, and its next window is what starts it
(`system/apps/browser/README.md`). Leave it for that window, or start it yourself with `POST
/browsers/<name>/start` on the service.

Then name every app and browser in the results message with what it was and where it now stands
(`references/results-message.md`), the same way the paused chats are named. An app you could not
start again is a caveat, not a silence.
