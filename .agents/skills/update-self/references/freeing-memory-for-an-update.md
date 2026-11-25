# Freeing memory so the update can finish

An update's heaviest moments -- launching the worker, its test gate, the frontend build -- are the
ones the memory guard sheds first. When that happens, this pass frees memory itself rather than
stopping to ask, within the licence below. It is the one flow that may: every other one offers the
candidates to the user and stops only what they approve (the `free-workspace-memory` skill).

## Read the memory the guard acts on, not the container's

<!-- CLEANUP: once update-self's floor, the oldest release the app updates from, reaches the first
release that ships workspace_memory.py, leave only its line below and drop the paragraph on the
earlier tool. -->

```bash
if [ -f system/services/oom_priority/bin/workspace_memory.py ]; then
    python3 system/services/oom_priority/bin/workspace_memory.py list --stoppable
elif [ -f system/services/oom_priority/bin/memory_candidates.py ]; then
    python3 system/services/oom_priority/bin/memory_candidates.py
else
    echo "This workspace's version has no memory listing."
fi
```

This is the workspace's own memory tool, whichever one its version has, never the target
release's: the tool reads what the running workspace wrote. Its first line is the free memory
**earlyoom decides on**: the headroom file the workspace publishes for `--host-meminfo` while it is
fresh, else `/proc/meminfo`, and it names which it read. Under a memory cap those two disagree, and
the container's own `/proc/meminfo` is the optimistic one -- it has read three times the real
headroom on a capped workspace. `free -m` reads that optimistic file, so do not decide anything
from it, and never tell the worker to rerun on a figure that came from it: the gate will be shed
again and the round is wasted.

The same line says how much of that free memory is left before the guard starts stopping
processes, which it does at a share of the total, not at zero: 1 GiB free on a 6.4 GiB
workspace is about 400 MiB of room. That room is what a heavy step can use, so judge by it.

Below that are two groups, each largest first: what can be stopped with nothing leaving the screen
(idle chats and background agents, apps no window shows, browsers no window shows), then the apps a
window shows. A row has its KIND, the NAME its stop takes, and its memory: roughly what stopping it
frees. A line starting `unknown:` means rows of that kind may be missing, and a `note:` line says
why. Unknown is not "nothing there", but stop only what is listed. Read the free memory again after
each round.

A workspace whose version has only the earlier tool, `memory_candidates.py`, gets the same things
laid out by kind. An app is stopped by its `PROGRAM`, and `AUTO-STOP` `no` is an app that keeps
running with no window. What it does not print is unknown, not absent: it lists no apps before
minds-v0.9.2, and before minds-v0.10.7 does not say how much free memory is left before the guard
starts stopping processes. With no memory listing at all, only chats can be stopped (`run-status
stop-chats --agent`); say so in the results message.

## What this pass may stop

**Every run** may stop what the Step 1 licence names: chats and background agents, the browser, and
the user apps no window is showing. The main chat is not among them: the chat app starts it again as
soon as it stops, so it is never listed.

**A scheduled run** may stop **any** `app` row of the listing, in either group. The listing is the
boundary, and it is drawn so that every app in it can be brought back:
it holds no critical app (the shell, the chat and the terminal are what the workspace is reached and
talked to through), nothing sharing a critical app's program, nothing internal, and nothing without
a supervisord program to start again.
Nothing outside the listing is yours to stop -- not a service, not the shell, not the user's data.

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

An app whose row reads `keeps running when none does` may be one
that does work between requests (a poller, a scheduled refresh, a job that outlives its window; the
`build-app` skill sets the field that way on purpose). It is still yours to stop, but prefer the
others first, and say in the results message that it was down for the update.

## Stop the program, never the windows

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status stop-apps <program> <program> ...
```

The programs are the NAME of the listing's `app` rows (the earlier tool's `PROGRAM`). Do **not**
quit an app through the shell, which closes its windows.

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
