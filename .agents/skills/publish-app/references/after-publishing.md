# After a version is published

What step 8 of SKILL.md does once the worker's branch is merged and the
publish is recorded, before you tell the user anything. The merge brought the
published version's changes into the workspace's own copy of the app, and
some of them take effect only when something is moved or started again.

The version is on GitHub by now. A problem you find here is fixed as any change
to the app is (the `update-app` skill) and goes out as the next version.

## Contents

- 1. The workspace's own packages
- 2. Stop the app, and move a skill's data
- 3. Start the app again
- 4. Check it on the user's data
- 5. What to tell the user

## 1. The workspace's own packages

When the app took in a skill, run `uv sync --all-packages --frozen` from the
repo root: a skill with a Python project of its own has left the workspace's
lock.

## 2. Stop the app, and move a skill's data

Skip steps 2 and 3 for an app with no `pyproject.toml` that took in no skill,
when `supervisorctl reread` names none of its programs: nothing it runs on
changed, and neither did how it is run. `reread` prints a line for each
program whose file differs from what supervisord runs, and starts or stops
nothing.

Stop the app's programs: `supervisorctl stop <name>`, and each
`<name>-<role>`.

When the app took in a skill, the worker's report names each directory the
skill's data moves between (`data/.skills/<skill>/` to a directory under
`data/.apps/<name>/`). Data is not in git, so the merge did not move it. Move
each one now (`mv`, never a copy: it can be large). An app's data is what an
update copies aside and an uninstall keeps, and only what is under
`data/.apps/<name>/` counts as that.

## 3. Start the app again

First have supervisord read the app's program files again, in case the merge
changed one (the worker regenerates them when it changes how the app runs):

```bash
supervisorctl reread && supervisorctl update
```

`update` starts each program whose file changed. Then start what is still
stopped (`supervisorctl status` shows which):

```bash
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart
```

starts the program named after the app, and is safe to run when it is already
up; `supervisorctl start <name>-<role>` starts each of the others.

An app's program installs the app's package as it starts. The published
version may run on another build of the app SDK than the one it ran on until
now (the worker moves an app from the workspace's own to the released one),
and the commands a skill it took in now runs are not installed until the app
has started. Started now, a problem shows while you are looking; left
running, the app would change over at its next restart, with nobody watching.

## 4. Check it on the user's data

Confirm the app still shows what it showed before, on the user's real data,
and that nothing new is in its log (the `check-app-errors` skill). When the
app took in a skill, run the skill once under the name the worker's report
gives it.

## 5. What to tell the user

- The app's address and the two ways to take it, as step 8 of SKILL.md says.
- A skill's new command, when it changed and they run it by name.
- Anything step 4 found, and that you are fixing it.

Nothing about packages, locks or what was restarted.
