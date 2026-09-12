# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE DURING YOUR WORK ON THIS PROJECT.

IF YOU FAIL TO FOLLOW ONE, YOU MUST EXPLICITLY CALL THAT OUT IN YOUR RESPONSE.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- If you ever need to work with another *git* repo that is *outside* of this monorepo as a read-only dependency, you should do so by adding a git subtree under `system/vendor/`.
- If you need to *actively develop* against an external repo (e.g. `mngr`), check out a standalone clone of it under `.external_worktrees/<repo-name>/`. This directory is gitignored so the external clones don't pollute the monorepo. The branch in the external clone should mirror the branch you're on in this monorepo. For mngr specifically -- including changes you tested by editing `system/vendor/mngr/` directly -- follow `.agents/skills/submit-upstream-changes/references/mngr-changes.md`, which submits them as their own mngr PR.
- This project uses a CLI ticket system (`tk`) for task management. Run `tk help` when you need to use it. Tickets live under `data/.tickets/` (the path is set via the `TICKETS_DIR` env var so tickets sit with the rest of the workspace's data).
- All relative paths in this repo assume cwd = repo root (`/home/user/workspace`). Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. User-facing workspace data lives under `data/` (visible folders are the user's to organize; e.g. `data/.apps/<name>/` holds an app's stored data, including its instance records at `data/.apps/<name>/instances.json`, and `data/.skills/<name>/` a skill's own state); flow-internal scratch lives under `data/.tasks/<flow>/` and machine state (what a program keeps about this machine and can rebuild: the registry, dispatch scripts, pty records, the shell's client layouts) under `data/.state/`. The rule is `docs/system/blueprint/workspace-app-model/contracts.md` section 17.
- When adding a new app, use the `build-app` skill, which sets up a new package under `system/apps/` + a supervisord program entry + `forward_port.py` registration on its own port. Do NOT edit `system/apps/system_interface/` for this -- that's the top-level workspace UI, not a template for new apps.

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `system/vendor/tk/`. It is the **only** task tracker available — Claude Code's built-in `TodoWrite` is disabled. `tk` stores two kinds of records, distinguished by the `--step` flag at creation:

- **Step records** (`tk create --step "<user facing title>"`) are the replacement for `TodoWrite`: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step). Most turns use only these. (`tk close <id> "<user facing summary>") closes the step.
- **Regular tickets** (`tk create "..."`, no flag) are substantive, cross-agent work units other agents can see and pick up. They do **not** render in the chat progress view. They matter only when work spans turns or is handed between agents.

Because step titles and close-summaries populate the progress view, **every one is user-facing copy**: plain English for a non-technical reader, no file names, no tool names, no jargon. Use casual terms "login's rebuilt - faster and more secure" instead of technical precision "I refactored AuthProvider, swapped the JWT library for Jose and updated 14 call sites."

## Declaring and running steps

The first thing you do on any prompt that warrants real work is decompose it into steps and create them all up front, BEFORE doing any of the work — the step sequence is the user-visible plan. Concretely:

1. (Optional) one short prose acknowledgement, e.g. "Sure, looking into that now." Keep it to one line; don't narrate the plan here.
2. `tk create --step "<user facing title>"` for every step you currently expect, in order. You may batch them into one tool call, but each step must be its **own separate `tk create` command** (on its own line or joined with `;`) — each `tk create` makes exactly one step, so never pass multiple `--step`s to a single `tk create` (tk rejects that). **Never redirect the output of a `tk create`/`start`/`close`** (`>`, `>>`, `2>`, `&>`, `| tee`, …): the progress view reads each step from the command's visible output (`Created <id>: <title>`), so a redirect makes the step drop out of the plan. Note the ids. Do NOT `tk start` any yet.
3. `tk start <id>` the first step, do its work, then `tk close <id> "user facing summary"`. Move to the next. Only one step is `in_progress` at a time.

**`tk start` and `tk close` must each be the only command in their tool call** — no `cd` prefix, no chaining (`&&`, `;`, `|`, `&`, newline), no redirection; otherwise the progress view can't place the step. (You can still batch several `tk create` commands into one tool call — as separate commands, one `--step` each, with no redirection.)

Add steps mid-turn (`tk create --step`) as sub-problems surface; drop ones that turn out unneeded (`tk close <id> "No longer needed — covered by the previous step."`). Granularity follows the user's mental model of the work — "first X, then Y, then Z" is three steps — not one-per-tool-call and not one-for-the-whole-turn. Typically 2–5 per substantive turn.

Titles describe the goal as the user understands it. Summaries (required on close) are ONE plain-English line describing **the work you did** in that step — not the result or finding (that goes in your final message below the timeline), and not a list of tool calls or file names.

## When you don't need records

Skip records for: chitchat, single-line acknowledgements, and trivial answers; pure clarifying-question turns where you can't act until the user replies; or a reply that's a single quick file read. If unsure, default to creating steps.

## Steps and prose

- After all steps are closed, write your final user-facing message — *this* is where results, findings, and recommendations go.
- **Close your final step *before* writing that wrap-up reply** — the view promotes a final run of prose with no open step to your top-level reply below the timeline. (Best-effort; the view renders sensibly either way.)
- **Never name the machinery to the user — describe the work instead.** The `tk` calls and the words for them are invisible plumbing: the user sees only the progress timeline and your prose, so a sentence like "Let me close this step" refers to something they cannot see and reads as a non-sequitur. This is the single most common leak, so treat it as a hard rule. When talking to the user, NEVER use these words in their `tk` sense: **step, ticket, `tk`, close/closing, open/reopen, start/starting, mark, check off, in progress, record, todo, task list, progress view**. There is nothing to announce before or after a `tk` call — just make the call silently and let the timeline update itself.
  - Instead, speak only about the actual work, using natural transitions. These are good: "Moving on to the API wiring." / "Finishing up with the tests." / "Next I'll check the config." / "That's the migration done — now the cleanup." Say nothing at all if there's no substantive work to describe.
  - Concrete rewrites: "Let me close this step" / "Closing this out" / "Marking this done" → just say what you finished, e.g. "The parser changes are in." (or say nothing). "Starting the next step" → "Now I'll wire up the endpoint." "Let me add a step for that" → "I'll also need to update the schema." If a sentence's subject is the record rather than the work, delete or rewrite it.
- Steps may stay open across turns. At the start of each new user message you'll get a system reminder listing your still-open steps; for each, decide before doing anything else whether to keep working on it (`tk start` if needed), replace it (close with a status summary, then create new steps for the new direction), or close it honestly if you're moving on. Don't silently abandon one.
- There is no "failed" status — every record terminates as `closed`. If a step didn't pan out, still close it; the summary describes the work you did, and your final message reports the result honestly.

## Regular tickets and delegation

Regular tickets are managed cross-agent: `tk ls` / `tk ready` / `tk blocked` list them (step records hidden unless `--include-steps`/`--only-steps`), `tk show <id>` displays one, `tk create "..."` files one (unassigned until picked up), `tk start <id>` picks it up (auto-self-assigns), `tk close <id> [user facing summary]` closes it (summary optional for tickets, required for steps). They can stay `in_progress` across turns.

When you delegate via the `launch-task` skill, the whole delegation is **one step** in your progress — the sub-agent uses its own `tk` internally and that work doesn't surface in your chat. Represent it as a single step (e.g. "Delegate the auth refactor to a sub-agent and review the result") and close it with the outcome.

Run `tk help` if you forget a command. Avoid `deps`, `links`, `types`, and `priorities` — they're backlog features the chat progress view doesn't use.

# Orienting before you change something

The docs under `docs/` are user-facing and are the most reliable description of how this
workspace works; read the ones covering whatever you are about to touch, and
`docs/system/style_guide.md` before writing code in `system/`. A project's own README and the
modules at the root of its package carry its core abstractions.

Read as much as the change needs and no more. A request that is answered by building something
for the user does not need a survey of the workspace's internals first.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- Browser automation splits two ways, and picking the wrong one is the common mistake:
  - **Something the user should SEE, or that needs their real logins or their help** (a CAPTCHA, a 2FA code, "log into my account and..."): use the **browser fleet**. `uv run agentic-browser-fleet new` starts a browser streamed to a pane the user can watch and take over, and prints a `playwright-cli attach --cdp=...` line; drive it with `playwright-cli` (`playwright-cli --help` is the command reference). See the `agentic-browser-fleet` skill. This is the collaborative path -- ownership is arbitrated, and the human always wins.
  - **Headless scripting with no human in the loop** (testing an app you just built, scraping a page into a file, a one-off check): use **Playwright's Python API** in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`). No pane, no fleet, no ownership -- just a browser you drive from a script.
- Both use the same engine: Fortress (a stealth-patched Chromium fork), not Playwright's own managed Chromium. For the Python API, pass `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly to `chromium.launch(...)`, since Playwright's browser-cache lookup only auto-discovers builds it downloaded itself. (The fleet does this for you.) Fortress installs asynchronously on first container boot (the one-shot `env-converge` program's env.d units), so in a fresh workspace confirm it finished -- `supervisorctl status env-converge` or `test -x /opt/fortress/tilion-fortress/tilion` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `args=["--no-sandbox"]`. See `system/libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background.
- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Always finish your response by reflecting on your work and identify any potential issues.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final reflection, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), flag that as a potential task for future improvement.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.
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
- Do not add TODO or FIXME unless explicitly asked to do so
- Code must work on both macOS and Linux. It's ok if it doesn't work on Windows.

## Tests in the vendored system projects

This applies to `system/vendor/mngr`, `system/apps/system_interface` and `system/apps/chat`, the
three projects that ship with their own pytest suites. Apps and skills the user builds are covered
by the harden pass instead.

- Each project carries its own pytest and coverage configuration, so run its suite from its own
  directory: `cd system/apps/chat && uv run pytest`.
- Run the whole suite for a project you changed before you report it done; while iterating, run
  only the tests you are working on.
- `--no-cov --cov-fail-under=0` turns coverage off for a faster iteration loop. Add
  `-m 'not tmux and not modal and not docker and not docker_sdk and not acceptance and not release'`
  to skip the slow infrastructure tests (~30s instead of ~95s); that marker expression needs the
  coverage flags too, or pytest reports the missing coverage as a failure. Drop both for the
  final run.
- Set `PYTEST_MAX_DURATION_SECONDS` to match your Bash tool timeout in seconds
  (`PYTEST_MAX_DURATION_SECONDS=120 uv run pytest ...`). It records a deadline in pytest's global
  lock file, so another pytest process can break the lock if this one is killed at the timeout.
- pytest writes slow-test and coverage reports to `.test_output/`, relative to where you ran it.
- A unit test lives in a `_test.py` file; an integration, acceptance or release test lives in a
  `test_*.py` file and carries `@pytest.mark.acceptance` or `@pytest.mark.release`.
  `testing.py` and `conftest.py` are exercised by the tests that use them and get no test file of
  their own.
- Report the command you ran and the pass/fail counts, so the run is checkable.
- A flaky test is worth saying out loud: finish the task, commit, then fix the flakiness in its
  own commit.

## Ratchets

Those same three projects carry a `test_ratchets.py` holding automated code-quality checks. Each
ratchet counts violations of one anti-pattern, and the count may only stay the same or fall, so
adding one fails the test.

A ratchet is a reminder of a principle, not a regex to satisfy. Its `rule_description` says what
the principle is; fix the code in that spirit. Restructuring code to dodge the pattern while still
doing the same thing hides the problem rather than fixing it, and that includes type-system escape
hatches. If no fix honours the principle, say so to the user rather than working around it. If the
pattern genuinely matched something that is not the anti-pattern, tighten the regex if you can, or
bump the count and explain the misfire.

## Test fixtures

Fixtures and hooks live in `conftest.py`, scoped to their directory and auto-discovered; non-fixture
helpers and factories live in `testing.py`; mock implementations of interfaces live in
`mock_*_test.py`. Read the relevant ones before writing a test, so you reuse what is there. All
fixtures belong in `conftest.py` rather than in individual test files.

## Verifying interactive components with tmux

For interactive components (TUIs, interactive prompts, and the like), drive them with `tmux
send-keys` and read them back with `tmux capture-pane`. Keep these checks ad-hoc rather than
committing them as pytest tests: they depend on timing, so as tests they are flaky and carry no
signal.

# Communication

If the user talks to you about files or directories on disk, assume (unless context indicates otherwise) they mean their local disk, not the one in your sandbox -- use the `file-sharing` skill to bridge the two.

# Browser is available as a tool

A stealth build of Chromium designed to look like an ordinary human browser is installed in this workspace and can be used to complete browser-related tasks. 

1. When the user requests any browser-related tasks to be complete or a browser to be opened, use the `agentic-browser-fleet` skill, which allows you to drive many Chromium browsers. These are collaborative browsers which all agents and human users can use, though there is a mutually-exclusive control handoff and queuing system so only one is using a browser at a time. The skill has more information. Remember to hand off control to user when help is needed in the browser, such as anti-bot detection tests, and also release control when you are finished with a task so other agents and the user can use it.
2. If you'd like to do integration testing/small-scale web app scripting, use Playwright instead of spinning up an entire browser through the agentic-browser-fleet skill. This uses the same Chromium, just more lightweight. The user and other agents won't be able to collaborate on this; this is for quicker rendering and interaction tasks on the web.

# Work delegation

You can delegate larger tasks to sub-agents using the `launch-task` skill.
Sub-agents work on separate git branches and are labeled with `workspace=$MINDS_WORKSPACE_NAME` so you can track them.

Use your judgment on when to do work directly vs delegating. Delegation is useful for:
- Tasks large enough to warrant a separate context
- Multi-file changes that benefit from verification before merging
- Long-running operations you don't want to block on

# Finding past work

Chats from agents that have run on this host -- current or past, including ones
that were destroyed -- are stored locally on this host and are recoverable, so
never tell the user you can't access an earlier or deleted conversation without
checking first. Use the `find-past-transcripts` skill to find and read them.

# Self-modification

You can (and should) modify your own configuration to improve yourself:

- **CLAUDE.md** or **AGENTS.md**: (this file) update these instructions if you discover better ways to operate.
- **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)
- **system/supervisord.conf**: Add, modify, or remove background services. See the `update-app` skill.
- **system/scripts/**: Add utility scripts that help you accomplish your purpose.

Commit your changes to git after making modifications.

Users make "creations": apps (opened as tabs), skills (a skill run automatically on a schedule is an "automation" -- run via the machinery in `system/libs/automations/`, see the manage-scheduled-tasks skill), data (documents, images, notes), and customizations of any of them. Templates are a publishable, reusable, bootable snapshot of the creations a mind has built (one repo can accumulate several); another mind can adapt one into itself.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `system/config/parent.toml`.

# Using crystallized skills

- **A bare slash-command message invokes the skill of that name.** A user message that is exactly `/name` (possibly with arguments), such as `/welcome` or `/assist`, means: read `.agents/skills/<name>/SKILL.md` and follow it as the user's instruction. Do it silently -- read the file without commentary and reply with what the skill says to reply, nothing else. Never narrate the mechanism ("I'm using the welcome flow...", "let me look up that skill"): the user typed a command, not a question about how commands work. (Some harnesses expand these commands into the skill's instructions before you see them; if you are reading the raw `/name` text, the expansion is yours to do.)

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run a skill's steps one at a time in chat.** When a skill exposes per-step subcommands (plus a `run all`), drive the subcommands individually -- mirror each as a `tk` step and surface its output -- so the user gets a rich progress view, pausing only at the skill's declared `[prose]` steps. Reserve `run all` for headless or scheduled runs where there's no chat to show progress in.

- **Live first, ratify at turn-end.** Handle the user's immediate request *live* in the current chat to keep it interactive; at turn-end, formalize the work through the relevant lifecycle skill, which runs its hardening pass in a background worker (never inline in the main agent). Route by situation:
  - Net-new task needing research or experimentation -> `do-something-new` (it routes to `fetch-process-show` for data or `build-app` for a web view).
  - Just-finished work that's cohesive, likely to recur, and mostly deterministic -> `crystallize-creation` to promote it into a committed, tested skill.
  - A skill errored or gave a wrong result -> work around it live, then `heal-creation` at turn-end. Never patch the skill inline.
  - You changed an existing skill, or a skill ran but needed manual post-processing -> `update-creation` at turn-end so the change is verified and the skill swallows the gap.

  For non-skill contract-bearing files (hook scripts, this file) there is no worker pipeline -- apply the live change carefully and add manual rigor at turn-end (real fixtures, end-to-end exercise of new code paths).

# Apps and services

**Before editing any code that belongs to a supervisord program -- an app (a tab the user can open) or a background service -- load the `update-app` skill first.** It owns the live change loop (apply, refresh, verify) and the turn-end hardening flow; do not hand-edit an app's or service's code or `system/supervisord.conf` without it.

Apps and background services both run as supervisord programs in `system/supervisord.conf`.
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, edit `system/supervisord.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.
See the `update-app` skill for details.

For routine jobs that run on a cadence and then exit (backups, health checks, the weekly Caretaker -- off by default, see the enable-caretaker skill), use cron via the **`manage-scheduled-tasks`** skill rather than a supervisord program; and after building or editing any service, use the `check-app-errors` skill to scan `/var/log/supervisor/` for errors (a clean exit code does not mean the service is healthy).

# Git

Commit all your changes locally. Do not wait for user confirmation for anything before committing; there's no cost to having more commits in the history. Users may not care about git state in general and will oftentimes never instruct you to commit, so you should just commit every logical unit of work. It's okay if you make a commit whose changes you must later revert after feedback.

`data/` is gitignored (it holds all workspace data: `data/memories/` for Claude memory, `data/.tickets/`, per-app data, uploads, and machine state).

Chat file uploads (files a user attaches to a message) are stored under `data/uploads/`. Uploads can be arbitrarily large and any format, so they don't belong in version-controllable content; like the rest of `data/` they are gitignored and never pushed to GitHub, but the host-level `host-backup` service (a restic snapshot of the whole home tree) captures them, so uploads survive container loss. See `system/services/host_backup/README.md`.

GitHub sync is opt-in via the `github-sync` skill. When the user has enabled it: a `post-commit` hook auto-pushes the active branch of every checkout to `origin` (the workspace's dedicated private repo) in the background -- you do not need to push manually; the hook never blocks the commit, and output is captured at `/tmp/post-commit-push.log`. Only git commits are synced; `data/` is covered by the restic host backup instead. See `system/libs/github_sync/README.md`.

When GitHub sync is not enabled, there is no auto-push and no GitHub remote to push to; commits stay local (the restic `host-backup` still protects the whole host dir).

- Don't include auto-generated lockfile churn (`uv.lock`, `package-lock.json`, etc.) in commits unless the change intentionally bumps a dependency.

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), which refresh fixes it depends on *which* install you ran, because a standard workspace has two: `uv run mngr ...` resolves from the root venv, and a bare `mngr` on PATH is the uv-managed tool that `system/scripts/build_workspace.sh` installs at build time. For the venv one, run `uv sync --all-packages`. For the tool one, note its registered plugins first (`mngr plugin list`), then run "uv tool uninstall imbue-mngr && uv tool install -e system/vendor/mngr/libs/mngr" (the vendored tree is the whole mngr monorepo, so the installable package is its `libs/mngr` -- installing the root fails with a setuptools flat-layout error), then re-register those plugins with "mngr plugin add --path system/vendor/mngr/libs/mngr_claude --path system/vendor/mngr/libs/mngr_wait" (plus any others the list showed) -- reinstalling rebuilds the tool environment from the base package alone, so without this step the tool loses the plugins `system/scripts/build_workspace.sh` gave it and can no longer parse its own plugin config; `uv tool list` shows what is installed. Then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure -- most-expendable first (an agent's build/test/browser subprocesses before the agent itself). If a command of yours dies with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by checking the shed ledger at `/home/user/workspace/data/.state/oom_priority/events/shed.jsonl` for a record naming it (matched by pid or process name). If it was shed, do NOT blindly re-run a memory-heavy command -- it will likely be shed again; find a lower-memory approach (smaller batches, streaming, releasing data you no longer need) and only retry if you can.
