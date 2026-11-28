# Dispatching the worker that gets a version ready

The task file and the commands for step 4 of SKILL.md. `<slug>` is `publish-`
followed by the app's name (`publish-notes`); the worker's branch is
`mngr/<slug>`. Run everything from the repo root of the live workspace.

If an earlier attempt left a worker or a branch of that name, remove it first:
"A worker an earlier attempt left" in `results.md`.

## The task file

The task file holds parameters only. The worker's instructions are in
`scan-worker.md`, a guide written for it and not for you.

````bash
mkdir -p data/.tasks/launch-task/<slug>
cat > data/.tasks/launch-task/<slug>/task.md << 'TASK_EOF'
---
finish_report_path: data/.tasks/launch-task/<slug>/reports/report.md
---

# Task: Get the "<name>" app ready to publish

Read `.agents/skills/publish-app/references/scan-worker.md` in your checkout
and follow it.

- app: <name>
- package: <package>
- version: <version>
- last published: <published_version, or "never: this is the first publish">
- repository: https://github.com/<owner>/<repo_name>
- leave out of what is published: <one glob per line, relative to the app's directory, or "nothing more">
- skills to take in: <each skill, then the paths that also use it, or "none">
- derived from: <upstream_url, for a modified app, or "nothing">
- context: <what the app does for its user, the accounts and services it
  depends on, known gaps -- what the worker needs to write good guidance for
  whoever installs it, and a good README>
TASK_EOF
````

`skills to take in` is what `skills-the-app-needs.md` had you decide ("In the
worker's task"); `none` when step 1 of SKILL.md did not send you there.

## Launch it, and wait for its report

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name <slug> --template worker \
    --runtime-dir data/.tasks/launch-task/<slug>/ \
    --task-file data/.tasks/launch-task/<slug>/task.md
```

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name <slug> --task-file data/.tasks/launch-task/<slug>/task.md
```

The wait returns at once and the report starts a later turn of yours: go back
to step 4 of SKILL.md for what to do with it.
