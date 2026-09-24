# Workspace rules for a worker

**This file is the subset of the top-level `AGENTS.md` that applies to you.
Read it and ignore that one.** `AGENTS.md` is loaded into your context
automatically and is written for an agent that holds a chat with the user and
owns a whole task; you are a node of a build, with one subtask, one report and
no chat. Everything here is copied from it, with the parts meant for the other
kind of agent taken out.

Your brief is your task file and the handoffs quoted inside it. Read those and
`.agents/skills/build-app-parallel/references/worker-node.md`, then read of the
rest of the repo only what your own subtask needs.

You launch no agents of your own.

These rules cover every kind of node a plan can name, so some of what follows is
not yours. If your subtask is to build a piece rather than to check one, skip
**Test fixture discovery**, **Manual verification and testing**, and the bullets
about running and writing tests in **When coding** -- your check is the single
command `worker-node.md` names, and the suite, coverage, ratchets and the review
gates belong to the hardening pass at the end of the build. Read those sections
only if your task file asks you to verify something or to write tests.

# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE AND IN YOUR TASK FILE.

IF YOU FAIL TO FOLLOW ONE, SAY SO EXPLICITLY IN YOUR REPORT.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- This project uses a CLI ticket system (`tk`) for task management. Run `tk help` when you need to use it. Tickets live under `data/.tickets/` (the path is set via the `TICKETS_DIR` env var so tickets sit with the rest of the workspace's data).
- All relative paths in this repo assume cwd = the root of the checkout you are working in -- for you that is the build folder you were started in, not `/home/user/workspace`. Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. User-facing workspace data lives under `data/` (visible folders are the user's to organize; e.g. `data/.apps/<name>/` holds an app's stored data, including its instance records at `data/.apps/<name>/instances.json`, and `data/.skills/<name>/` a skill's own state); flow-internal scratch lives under `data/.tasks/<flow>/` and machine state (what a program keeps about this machine and can rebuild: the registry, dispatch scripts, pty records, the shell's client layouts) under `data/.state/`. The rule is `docs/system/blueprint/workspace-app-model/contracts.md` section 17.

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `system/vendor/tk/`. It is the **only** task tracker available — Claude Code's built-in `TodoWrite` is disabled. `tk` stores two kinds of records, distinguished by the `--step` flag at creation:

- **Step records** (`tk create --step "<user facing title>"`) are the replacement for `TodoWrite`: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step; that chat is the orchestrating agent's, not yours). Most turns use only these. (`tk close <id> "<user facing summary>") closes the step.
- **Regular tickets** (`tk create "..."`, no flag) are substantive, cross-agent work units other agents can see and pick up. They do **not** render in the chat progress view. They matter only when work spans turns or is handed between agents.

Because step titles and close-summaries populate the progress view -- the orchestrating agent's, which the user reads -- **every one is user-facing copy**: plain English for a non-technical reader, no file names, no tool names, no jargon. Use casual terms "login's rebuilt - faster and more secure" instead of technical precision "I refactored AuthProvider, swapped the JWT library for Jose and updated 14 call sites." The vocabulary for all user-facing text (titles, summaries, and your prose alike) is `.agents/shared/references/user-facing-language.md`.

When you are following a skill, mirror its rough sequence in the progress view, not its exact one (the orchestrating agent shows the user stages; your records sit under the one you are part of). A skill's steps are written for you and are usually finer than the user's mental model: collapse several into one title when the user would see them as one thing ("Connect to GitHub" covers requesting the permission, verifying it, and wiring it up), and never lift an internal step name into a title.

## Declaring and running steps

The first thing you do on any prompt that warrants real work is decompose it into steps and create them all up front, BEFORE doing any of the work. Concretely:

1. `tk create --step "<user facing title>"` for every step you currently expect, in order. You may batch them into one tool call, but each step must be its **own separate `tk create` command** (on its own line or joined with `;`) — each `tk create` makes exactly one step, so never pass multiple `--step`s to a single `tk create` (tk rejects that). **Never redirect the output of a `tk create`/`start`/`close`** (`>`, `>>`, `2>`, `&>`, `| tee`, …): the progress view reads each step from the command's visible output (`Created <id>: <title>`), so a redirect makes the step drop out of the plan. Note the ids. Do NOT `tk start` any yet.
2. `tk start <id>` the first step, do its work, then `tk close <id> "user facing summary"`. Move to the next. Only one step is `in_progress` at a time.

**`tk start` and `tk close` must each be the only command in their tool call** — no `cd` prefix, no chaining (`&&`, `;`, `|`, `&`, newline), no redirection; otherwise the progress view can't place the step. (You can still batch several `tk create` commands into one tool call — as separate commands, one `--step` each, with no redirection.)

Add steps mid-turn (`tk create --step`) as sub-problems surface; drop ones that turn out unneeded (`tk close <id> "No longer needed — covered by the previous step."`). Granularity follows the user's mental model of the work — "first X, then Y, then Z" is three steps — not one-per-tool-call and not one-for-the-whole-turn. Typically 2–5 per substantive turn.

Titles describe the goal as the user understands it. Summaries (required on close) are ONE plain-English line describing **the work you did** in that step — not the result or finding (that goes in your report), and not a list of tool calls or file names.

## When you don't need records

Skip records for trivial work -- a single quick file read, and nothing else to do. If unsure, default to creating steps.

## Steps and prose

- **Never name the machinery to the user — describe the work instead.** (You never address the user yourself; the orchestrating agent does. This governs the words you put in a title or a summary, which reach the user through it.) The `tk` calls and the words for them are invisible plumbing: the user sees only the progress timeline and your prose, so a sentence like "Let me close this step" refers to something they cannot see and reads as a non-sequitur. This is the single most common leak, so treat it as a hard rule. When talking to the user, NEVER use these words in their `tk` sense: **step, ticket, `tk`, close/closing, open/reopen, start/starting, mark, check off, in progress, record, todo, task list, progress view**. There is nothing to announce before or after a `tk` call — just make the call silently and let the timeline update itself. The same hard rule covers git (see "Git" below): **commit, branch, push, merge, PR, rebase, checkout, diff, repo** are plumbing words too.
  - Instead, speak only about the actual work, using natural transitions. These are good: "Moving on to the API wiring." / "Finishing up with the tests." / "Next I'll check the config." / "That's the migration done — now the cleanup." Say nothing at all if there's no substantive work to describe.
  - Concrete rewrites: "Let me close this step" / "Closing this out" / "Marking this done" → just say what you finished, e.g. "The parser changes are in." (or say nothing). "Starting the next step" → "Now I'll wire up the endpoint." "Let me add a step for that" → "I'll also need to update the schema." If a sentence's subject is the record rather than the work, delete or rewrite it.
- There is no "failed" status — every record terminates as `closed`. If a step didn't pan out, still close it; the summary describes the work you did, and your final message reports the result honestly.

## Regular tickets and delegation

Regular tickets are managed cross-agent: `tk ls` / `tk ready` / `tk blocked` list them (step records hidden unless `--include-steps`/`--only-steps`), `tk show <id>` displays one, `tk create "..."` files one (unassigned until picked up), `tk start <id>` picks it up (auto-self-assigns), `tk close <id> [user facing summary]` closes it (summary optional for tickets, required for steps). They can stay `in_progress` across turns.

Run `tk help` if you forget a command. Avoid `deps`, `links`, `types`, and `priorities` — they're backlog features the chat progress view doesn't use.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- **Headless scripting with no human in the loop** (testing an app you just built, scraping a page into a file, a one-off check): use **Playwright's Python API** in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`). No pane, no fleet, no ownership -- just a browser you drive from a script.
- The browser here is Fortress (a stealth-patched Chromium fork), not Playwright's own managed Chromium. For the Python API, pass `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly to `chromium.launch(...)`, since Playwright's browser-cache lookup only auto-discovers builds it downloaded itself. (The fleet does this for you.) Fortress installs asynchronously on first container boot (the one-shot `env-converge` program's env.d units), so in a fresh workspace confirm it finished -- `supervisorctl status env-converge` or `test -x /opt/fortress/tilion-fortress/tilion` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `args=["--no-sandbox"]`. See `system/libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / status line / web) -- just propose the web view and only deviate when there's a specific reason (e.g. CLI for batch jobs).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records. *Preserve*: durably persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever gets back to the origin) -- not just in memory for the current run; don't fetch-transform-discard, so a later change in processing needs no refetch. *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask -- so they can bridge any gap the derived view leaves. **Render the raw record in its native format** (HTML email as the rendered email, JSON pretty-printed, markdown rendered -- not escaped source text); "raw" means *unprocessed by your derivation*, not *unrendered*. Build these affordances in by default but **keep them subtle** -- don't announce in chat that you're saving data or adding a "view raw" control.
- **Naming is informative, not cheeky.** Service names, app names, skill names, command names: prefer something that explains what the thing does (`slack-inbox-checker`) over something clever (`nothing-new`). Cute names tax every later mention.
- **Platform-internal APIs are valid.** Don't restrict yourself to officially documented public APIs. If a platform's own client (web app, mobile app) uses internal or undocumented endpoints to do something, those endpoints are fair game -- inspect what the official client actually calls and use the same endpoints with the same user-session auth. This is often cleaner than designing brute-force workarounds on top of a limited public API.

# When coding, follow these guidelines:

- Only make the changes that are necessary for the current task.
- Before implementing something, check if there is something in the codebase or look for a library
- Reuse code and use external dependencies heavily. Before implementing something, make sure that it doesn't already exist in the codebase, and consider if there's a library that can be imported instead of implementing it yourself. We want to be able to maintain the minimum amount of code that gets the job done, even if that means introducing dependencies. If you don't know of a library but think one might be plausible, search the web. (I'm even open to using random GitHub projects, but run anything that's not a well-established library by me first so I can check if it's likely to be reliable.)
- Code quality is extremely important. Do not compromise on quality to deliver a result--if you don't know a good way to do something, ask.
- Follow the style guide!
- Use the power of the type system to constrain your code and provide some assurance of correctness. If some required property can't be guaranteed by the type system, it should be runtime checked (i.e. explode if it fails).
- Avoid using the `TYPE_CHECKING` guard. Do not add it to files that do not already contain it, and never put imports inside of it yourself--you MUST ask for explicit permission to do this (it's generally a sign of bad architecture that should be fixed some other way).
- Do NOT write code in `__init__.py`--leave them completely blank (the only exception is for a line like "hookimpl = pluggy.HookimplMarker("mngr")", which should go at the very root __init__.py of a library).
- Do NOT make constructs like module-level usage of `__all__`
- To run tests for a single project: "cd system/vendor/mngr && uv run pytest", "cd system/apps/system_interface && uv run pytest", or "cd system/apps/chat && uv run pytest". Each project has its own pytest and coverage configuration in its pyproject.toml.
- While you're iterating, you can pass "--no-cov --cov-fail-under=0" to disable coverge (slightly faster), but during your final check, you *MUST NOT* pass those flags (it will fail in CI anyway)
- For faster iteration, add "-m 'not tmux and not modal and not docker and not docker_sdk and not acceptance and not release'" to skip slow infrastructure tests (~30s instead of ~95s). These still run in CI. Note that you *MUST* also pass "--no-cov --cov-fail-under=0" when doing this, otherwise it will complain about a lack of coverage.
- If you need to run a specific acceptance or release test to write or fix it, iterate on that specific test locally by calling "just test <full_path>::<test_name>" from the root of the git checkout. Do this rather than re-running all tests in CI.
- If tests fail because of a lack of coverage, you should add tests for the new code that you wrote.
- When adding tests, consider whether it should be a unit test (in a _test.py file) or an integration/acceptance/release test (in a test_*.py file, and marked with @pytest.mark.acceptance or @pytest.mark.release, no marks needed for integration).  See the style_guide.md for exact details on the types of tests. In general, most slow tests of all functionality should be release tests, and only important / core functionality should be acceptance tests.
- Do NOT create tests for test utilities (e.g. never create `testing_test.py`). Code in `testing.py` and `conftest.py` is exercised by the tests that use it and does not need its own test file.
- Do NOT create tests that code raises NotImplementedError.
- If you see a flaky test, you must not let it pass silently: fix it as soon as possible, and say so in your report.
- Do not add TODO or FIXME unless explicitly asked to do so
- Code must work on both macOS and Linux. It's ok if it doesn't work on Windows.
- To reiterate: code correctness and quality is the most important concern when writing code.

## Test fixture discovery

Before writing new tests, read the relevant `conftest.py` and `testing.py` files to avoid reimplementing things that already exist. 
Test infrastructure lives in these files:

| File pattern | Purpose |
|---|---|
| `conftest.py` | Pytest fixtures and hooks, scoped to the directory they're in (auto-discovered by pytest) |
| `testing.py` | Non-fixture test utilities: factory functions, helpers, context managers (explicitly imported) |
| `mock_*_test.py` | Concrete mock implementations of interfaces (explicitly imported) |

All fixtures must be in conftest.py, not in individual test files.

# Manual verification and testing

Before declaring any feature complete, manually verify it: exercise the feature exactly as a real user would, with real inputs, and critically evaluate whether it *actually does the right thing*. 
Do not confuse "no errors" with "correct behavior" -- a command that exits 0 but produces wrong output is not working.

Then crystallize the verified behavior into formal tests. 
Assert on things that are true if and only if the feature worked correctly -- this ensures tests are both reliable and meaningful.

## Verifying interactive components with tmux

For interactive components (TUIs, interactive prompts, etc.), use `tmux send-keys` and `tmux capture-pane` to manually verify them. 
This is a special case: do NOT crystallize these into pytest tests. 
They are inherently flaky due to timing and useless in CI, but valuable for agents to verify that interactive behavior looks right during development.

# Using crystallized skills

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run the creation, don't redo its job.** When a creation already does what's being asked -- an app that ingests this kind of data, a skill that runs this process -- run it, or extend it and run it. Producing the same result by hand beside it leaves the creation untested against the real case and the user with two sources of truth.

- **Live first, ratify at turn-end.** Work is done first and formalized afterwards, through the relevant lifecycle skill, which runs its hardening pass in a background worker (never inline). Route by situation:
  - Net-new task needing research or experimentation -> `do-something-new` (it routes to `fetch-process-show` for data or `build-app-parallel` for a web view).
  - Just-finished work that's cohesive, likely to recur, and mostly deterministic -> `crystallize-creation` to promote it into a committed, tested skill.
  - A skill errored or gave a wrong result -> work around it live, then `heal-creation` at turn-end. Never patch the skill inline.
  - You changed an existing skill, or a skill ran but needed manual post-processing -> `update-creation` at turn-end so the change is verified and the skill swallows the gap.

  For non-skill contract-bearing files (hook scripts, this file) there is no worker pipeline -- apply the live change carefully and add manual rigor at turn-end (real fixtures, end-to-end exercise of new code paths).

  In a build you are a node of, the turn-end handoff is not yours: after the
  last node the orchestrating agent makes the single `crystallize-creation`
  call for the whole app. Report what you built and stop.

- **A change to hardened code carries its tests.** When you change code a harden pass already covered, extend that code's tests in the same commit. Code a change leaves untested is a regression even when it works.
  (Only where your subtask asks you to touch that code -- see "When coding".)

# Apps and services

Apps and background services both run as supervisord programs, each declared in its own `system/supervisord.conf.d/<name>.conf` (pulled in by an `[include]` glob in `system/supervisord.conf`, which holds only the daemon's own config).
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, add/edit/delete its own `system/supervisord.conf.d/<name>.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.

# Git

`data/` is gitignored (it holds all workspace data: `data/memories/` for Claude memory, `data/.tickets/`, per-app data, uploads, and machine state).


# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), which refresh fixes it depends on *which* install you ran, because a standard workspace has two: `uv run mngr ...` resolves from the root venv, and a bare `mngr` on PATH is the uv-managed tool that `system/scripts/build_workspace.sh` installs at build time. For the venv one, run `uv sync --all-packages`. For the tool one, run `python3 system/scripts/install_mngr.py` -- the same program the build runs, which reinstalls it with the plugins `system/config/mngr_plugins.toml` assigns it, into the pinned tool directory the build installs into and puts first on `PATH`. If `mngr` on your PATH turns out to come from somewhere else instead (a workspace created before that pin has a second copy under `/home/user/.local`), follow it with `python3 system/scripts/tool_env.py drop-shadowing-mngr`, which is what the build runs next and what leaves the refreshed install the one a login shell reaches. Do not hand-roll the `uv tool install`: the ways that goes wrong (installing under the wrong `$HOME`, or with no plugins, leaving an mngr that cannot parse `[agent_types.*]`) are exactly what that program exists to prevent. `uv tool list` shows what is installed. Then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure -- most-expendable first (an agent's build/test/browser subprocesses before the agent itself). If a command of yours dies with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by checking the shed ledger at `/home/user/workspace/data/.state/oom_priority/events/shed.jsonl` for a record naming it (matched by pid or process name). If it was shed, do NOT blindly re-run a memory-heavy command -- it will likely be shed again; find a lower-memory approach (smaller batches, streaming, releasing data you no longer need) and only retry if you can.

# Sandboxed runtime

Remote (imbue_cloud) workspaces -- and Linux desktop workspaces -- run their container under gVisor (`runsc`), a user-space kernel that sits between the container and the host kernel (`uname -r` reports `4.19.0-gvisor`). Most software is unaffected, but some things do not work inside the sandbox: ptrace-based tooling (`strace`, `gdb` attach, `perf`), eBPF, FUSE mounts, `io_uring`, nested container runtimes (running docker/podman inside the workspace), and unusual `ioctl`s. Filesystem-metadata-heavy operations (`find`, `tar`, `git status` over large trees) are several times slower than on a plain kernel, and interpreter startup is somewhat slower. Do not try to install or "fix" any of these -- work around them (e.g. `--no-sandbox` for Chromium, logging instead of `strace`) and tell the user when a tool is unavailable for this reason.
