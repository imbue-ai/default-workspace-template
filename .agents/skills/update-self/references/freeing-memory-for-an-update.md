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
talked to through), nothing internal, and nothing without a supervisord program to start again.
Nothing outside it is yours to stop -- not a service, not the shell, not the user's data.

Tell the two apart by who dispatched the run. The app writes this before it creates the chat:

```bash
python3 -c "
import json, os
try:
    dispatch = json.load(open('data/.state/update-apply/last-dispatch.json'))
except (OSError, ValueError):
    dispatch = {}
is_mine = dispatch.get('chat_agent_name') == os.environ.get('MNGR_AGENT_NAME') or dispatch.get(
    'chat_id'
) == os.environ.get('MNGR_AGENT_ID')
print(dispatch.get('started_by') if is_mine else None)
"
```

`scheduled` is the broader licence. Anything else -- `immediate`, `None`, a file that is not this
run's -- is a pressed "Update now", where the user is at the screen and an app going dark under them
is a surprise, so the narrower rule stands and a windowed app is still theirs to approve. Read the
file rather than the run record: Step 1's `run-status start` copies `started_by` onto `run.json`,
but it runs from the workspace's own, possibly older, copy of this flow, which may not know the
field.

Stop as little as the run needs. Take the candidates largest first, re-read the free memory, and
stop when there is room -- a scheduled licence is not a reason to clear the workspace.

An app the listing marks `stop_when_no_windows: false` may be one that does work between requests (a
poller, a scheduled refresh, a job that outlives its window; the `build-app` skill sets the field
that way on purpose). It is still yours to stop, but prefer the others first, and say in the results
message that it was down for the update.

## Stop the program, never the windows

`supervisorctl stop <program>` is the whole stop. Do **not** quit the app through the shell: a quit
closes every window of it first, and the pass would then owe the user a screen it took apart.

A stopped program leaves its windows where they are. The shell parks the app's port and answers that
window "Starting <app>" if its page reloads -- and that connection wakes the app again
(`system_interface/shell/app_lifecycle.py`). So an app someone is actually looking at comes straight
back, which is the right behaviour and also why the headroom has to be read again rather than
assumed. On an unattended run nothing reloads and the app stays down.

If a window did get closed -- by this pass, or by an app that tore its own down -- open it again at
the end (`uv run --no-sync workspace-layout open <app>`), because nothing else will.

## Putting it back

A landed apply needs nothing: it runs `mngr start --restart system-services`, which restarts
supervisord, and every program carries `autostart=true`, so they all come up again. That is also why
a stopped app costs the user only the stretch before the restart.

A pass that ends any other way -- a rollback, `STUCK`, `REFUSED`, a decision for the user -- reaches
no restart, so start each app you stopped by name before you compose the results message:

```bash
supervisorctl start <program> ...
```

Then name every app in the results message with what it was and that it is back
(`references/results-message.md`), the same way the paused chats are named. An app you could not
start again is a caveat, not a silence.
