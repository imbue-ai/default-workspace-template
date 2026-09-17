# Working as one node of an app build

You are one node of a plan for building an app. An orchestrating agent launched
you and several other workers into the same folder: a git checkout created for
this one build. Your task file names your subtask and gives you the reports of
the nodes you depend on. This document is the rest of your task.

## Do only your subtask

Your subtask is the whole of your job. The rest of the app is other nodes' work:
some are building it right now beside you, and some start after you report.
Doing any of it yourself collides with them, or builds a second version of
something the plan already assigns.

1. **Do exactly what the subtask asks, and stop.** When its hand-back is ready,
   report. Do not go on to the next thing the app needs, however obvious or
   quick it looks.
2. **Leave out everything the subtask does not name.** If it does not say to
   scaffold, do not scaffold. If it does not say to build the mock, the page,
   the routes, the storage or the icon, do not build them. Do not wire pieces
   together, verify the whole app, add tests, or tidy, refactor or restyle files
   you were not given.
3. **If something you need is missing, report `stuck`.** An earlier node's output
   that is not in the folder, or a contract its report does not give, is a
   problem for the orchestrator. Do not build the missing piece yourself.
4. **If the subtask looks wrong or incomplete, do it as written** and say in your
   report what you would change. Do not widen it.

## The shared folder

Other workers are editing this folder while you work, and nothing warns either of
you when two of you touch the same file.

1. **Edit only the files your subtask gives you.** Your subtask names what you
   own (a module, the page template, the static files). Create new files freely
   inside that boundary. If the work genuinely needs a change to a file outside
   it, do not make the change: describe it in your report and let the
   orchestrator schedule it.
2. **Leave git alone.** No `git commit`, `add`, `stash`, `checkout`, `reset`,
   `merge` or anything else that changes git state. The orchestrator commits the
   folder when no worker is running. Reading (`git status`, `git diff`,
   `git log`) is fine.
3. **Leave the shared registration files alone** -- the root `pyproject.toml`,
   `uv.lock` and `system/supervisord.conf` -- unless your subtask is the one that
   scaffolds the app or adds its libraries. If it is, run
   `uv sync --all-packages` after changing them.
4. **Do not touch the running workspace.** Apps run under supervisord from the
   workspace's main checkout, not from this folder. Do not run `supervisorctl`,
   `system/scripts/forward_port.py` or `system/scripts/layout.py`, and do not
   open tabs. The orchestrator shows the user a preview and takes the app live
   after the build.

## How to build

`.agents/skills/build-app/SKILL.md` describes how a whole app is built here, from
the first question to the hardening handoff. Use it only as a reference for *how*
to do your subtask: the scaffolder, file-path conventions, the `frontend-design`
skill before any markup, raw-data affordances. It never tells you *what* to do.
Read the parts that cover your subtask and follow their mechanics; ignore its
order of steps and everything outside your subtask. Its steps for the main agent
-- running the plan recorder, asking the user questions, showing the mock,
surfacing the tab, and handing off to `crystallize-creation` -- are never yours.

Where you meet something you would rather ask about (a name, a default, an
ambiguity), decide, and say what you decided in your report.

You never talk to the user, and you never start anything that asks the user for
something, such as a `latchkey` permission request. Every contact with the user
is a separate node the orchestrator runs. If your subtask needs access or an
answer the user has not given, report `stuck` and name what is needed.

## How to check your work

Do a quick check of your own piece and nothing more: it imports, it runs, it
serves, the page renders. To run the app from this folder without touching the
live one, start a throwaway instance on a spare port:

```bash
python3 .agents/shared/scripts/serve_isolated_instance.py up --name <slug> \
    --cwd "$PWD" --port-env <PACKAGE_UPPER>_PORT \
    --env <PACKAGE_UPPER>_DATA_DIR=<scratch dir> -- uv run <app-name>
# ... curl it or load it with Playwright ...
python3 .agents/shared/scripts/serve_isolated_instance.py down --name <slug>
```

Always bring it down before you report. Do not write a test suite, and do not run
the full test suite, coverage, ratchets, `/autofix` or any review gate, even
where `CLAUDE.md` asks for them. One hardening pass runs all of those once the
whole app is built.

## Reporting back

Your task file's frontmatter holds `finish_report_path`, and the launcher added
`lead_agent`. Follow `.agents/shared/references/worker-reporting.md` to write and
deliver your report, with these substitutions:

- `<TASK_FILE_GLOB>` -> the exact task file path named at the top of your task
  (not a glob: every node's task file is in this folder)
- `<RUNTIME_REPORTS_DIR>` -> `dirname "$FINISH_REPORT_PATH"`
- Valid `name:` values: `done` or `stuck`, with `type: status`.

The body of a `done` report is the handoff the nodes after you read. Include:

1. **What you built**, in two or three sentences.
2. **Files you created or changed**, as a list of paths.
3. **Contracts** later nodes build against: routes and what they return, module
   functions, data shapes, the app name, package folder and port.
4. **Decisions you made** that were not in your subtask.
5. **What you left stubbed or undone**, and any change you need outside your
   boundary or would make to your subtask.
6. **How to see it**: the command you used for your quick check.

Use `stuck` when you cannot finish the subtask: say why in one or two sentences
and what you would need.

## After a `done` report

Stop your turn. If you built something the user reviews (the mock or the working
site), the orchestrator may message you with changes the user asked for. Apply
them inside your boundary, check them, and deliver a fresh report the same way.
