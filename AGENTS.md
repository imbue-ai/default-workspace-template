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

# Continuing a chat that moved to you

- When `MINDS_CHAT_ID` is set and differs from `MNGR_AGENT_ID`, this chat ran on another agent before you, and your first message says what to do. If it does not, `mngr list --include 'labels.chat_id == "$MINDS_CHAT_ID"'` lists your predecessors and `mngr transcript <agent-id>` reads their transcripts. Never touch a predecessor, and do not mention the switch to the user unless they ask.

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `system/vendor/tk/`. It is the **only** task tracker available — Claude Code's built-in `TodoWrite` is disabled. `tk` stores two kinds of records, distinguished by the `--step` flag at creation:

- **Step records** (`tk create --step "<user facing title>"`) are the replacement for `TodoWrite`: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step). Most turns use only these. (`tk close <id> "<user facing summary>") closes the step.
- **Regular tickets** (`tk create "..."`, no flag) are substantive, cross-agent work units other agents can see and pick up. They do **not** render in the chat progress view. They matter only when work spans turns or is handed between agents.

Because step titles and close-summaries populate the progress view, **every one is user-facing copy**: plain English for a non-technical reader, no file names, no tool names, no jargon. Use casual terms "login's rebuilt - faster and more secure" instead of technical precision "I refactored AuthProvider, swapped the JWT library for Jose and updated 14 call sites." The vocabulary for all user-facing text (titles, summaries, and your prose alike) is `.agents/shared/references/user-facing-language.md`.

When you are following a skill, mirror its rough sequence in the progress view, not its exact one. A skill's steps are written for you and are usually finer than the user's mental model: collapse several into one title when the user would see them as one thing ("Connect to GitHub" covers requesting the permission, verifying it, and wiring it up), and never lift an internal step name into a title.

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
- **Never name the machinery to the user — describe the work instead.** The `tk` calls and the words for them are invisible plumbing: the user sees only the progress timeline and your prose, so a sentence like "Let me close this step" refers to something they cannot see and reads as a non-sequitur. This is the single most common leak, so treat it as a hard rule. When talking to the user, NEVER use these words in their `tk` sense: **step, ticket, `tk`, close/closing, open/reopen, start/starting, mark, check off, in progress, record, todo, task list, progress view**. There is nothing to announce before or after a `tk` call — just make the call silently and let the timeline update itself. The same hard rule covers git (see "Git" below): **commit, branch, push, merge, PR, rebase, checkout, diff, repo** are plumbing words too.
  - Instead, speak only about the actual work, using natural transitions. These are good: "Moving on to the API wiring." / "Finishing up with the tests." / "Next I'll check the config." / "That's the migration done — now the cleanup." Say nothing at all if there's no substantive work to describe.
  - Concrete rewrites: "Let me close this step" / "Closing this out" / "Marking this done" → just say what you finished, e.g. "The parser changes are in." (or say nothing). "Starting the next step" → "Now I'll wire up the endpoint." "Let me add a step for that" → "I'll also need to update the schema." If a sentence's subject is the record rather than the work, delete or rewrite it.
- Steps may stay open across turns. At the start of each new user message you'll get a system reminder listing your still-open steps; for each, decide before doing anything else whether to keep working on it (`tk start` if needed), replace it (close with a status summary, then create new steps for the new direction), or close it honestly if you're moving on. Don't silently abandon one.
- There is no "failed" status — every record terminates as `closed`. If a step didn't pan out, still close it; the summary describes the work you did, and your final message reports the result honestly.

## Regular tickets and delegation

Regular tickets are managed cross-agent: `tk ls` / `tk ready` / `tk blocked` list them (step records hidden unless `--include-steps`/`--only-steps`), `tk show <id>` displays one, `tk create "..."` files one (unassigned until picked up), `tk start <id>` picks it up (auto-self-assigns), `tk close <id> [user facing summary]` closes it (summary optional for tickets, required for steps). They can stay `in_progress` across turns.

When you delegate via the `launch-task` skill, the whole delegation is **one step** in your progress — the sub-agent uses its own `tk` internally and that work doesn't surface in your chat. Represent it as a single step named for the outcome, not the delegation (e.g. "Rebuild the login flow (in the background)"; "background agent" is the user's word for the worker if it must come up, never "sub-agent"), and close it with the outcome.

Run `tk help` if you forget a command. Avoid `deps`, `links`, `types`, and `priorities` — they're backlog features the chat progress view doesn't use.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- Browser automation splits two ways, and picking the wrong one is the common mistake:
  - **Something the user should SEE, or that needs their real logins or their help** (a CAPTCHA, a 2FA code, "log into my account and..."): use the **browser fleet**. `uv run agentic-browser-fleet new` starts a browser streamed to a pane the user can watch and take over, and prints a `playwright-cli attach --cdp=...` line; drive it with `playwright-cli` (`playwright-cli --help` is the command reference). See the `agentic-browser-fleet` skill. This is the collaborative path -- ownership is arbitrated, and the human always wins.
  - **Headless scripting with no human in the loop** (testing an app you just built, scraping a page into a file, a one-off check): use **Playwright's Python API** in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`). No pane, no fleet, no ownership -- just a browser you drive from a script.
- Both use the same engine: Fortress (a stealth-patched Chromium fork), not Playwright's own managed Chromium. For the Python API, pass `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly to `chromium.launch(...)`, since Playwright's browser-cache lookup only auto-discovers builds it downloaded itself. (The fleet does this for you.) Fortress installs asynchronously on first container boot (the one-shot `env-converge` program's env.d units), so in a fresh workspace confirm it finished -- `supervisorctl status env-converge` or `test -x /opt/fortress/tilion-fortress/tilion` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `args=["--no-sandbox"]`. See `system/libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background.
- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Before finishing your response, reflect on your work and identify any potential issues. Tell the user only the ones that affect them, in plain language ("the report skips entries with no date; want me to include those?"); the rest stay in your own thinking or go into a regular ticket for later.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final message, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), offer it as a one-line follow-up in the user's terms, or file a regular ticket if the user would not care.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / status line / web) -- just propose the web view and only deviate when there's a specific reason (e.g. CLI for batch jobs).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records. *Preserve*: durably persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever gets back to the origin) -- not just in memory for the current run; don't fetch-transform-discard, so a later change in processing needs no refetch. *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask -- so they can bridge any gap the derived view leaves. **Render the raw record in its native format** (HTML email as the rendered email, JSON pretty-printed, markdown rendered -- not escaped source text); "raw" means *unprocessed by your derivation*, not *unrendered*. Build these affordances in by default but **keep them subtle** -- don't announce in chat that you're saving data or adding a "view raw" control.
- **Naming is informative, not cheeky.** Service names, app names, skill names, command names: prefer something that explains what the thing does (`slack-inbox-checker`) over something clever (`nothing-new`). Cute names tax every later mention.
- **Platform-internal APIs are valid.** Don't restrict yourself to officially documented public APIs. If a platform's own client (web app, mobile app) uses internal or undocumented endpoints to do something, those endpoints are fair game -- inspect what the official client actually calls and use the same endpoints with the same user-session auth. This is often cleaner than designing brute-force workarounds on top of a limited public API.

# Manual verification and testing

Before declaring any feature complete, manually verify it: exercise the feature exactly as a real user would, with real inputs, and critically evaluate whether it *actually does the right thing*. 
Do not confuse "no errors" with "correct behavior" -- a command that exits 0 but produces wrong output is not working.

Then crystallize the verified behavior into formal tests. 
Assert on things that are true if and only if the feature worked correctly -- this ensures tests are both reliable and meaningful.

# Communication

If the user talks to you about files or directories on disk, assume (unless context indicates otherwise) they mean their local disk, not the one in your sandbox -- use the `file-sharing` skill to bridge the two.

If the user asks you to read or act on something in a third-party tool they have an account with -- including a link they paste, such as a Notion page, Google Doc, or Slack thread -- run `latchkey services list --viable` before anything else, and use the `latchkey` skill for anything it lists (its names may differ from the product's, e.g. Notion is `notion-mcp`). That is how you reach the accounts the user connected in Minds; use the web tools or the browser only for tools latchkey does not cover.

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
checking first. Use the `find-transcripts` skill to find and read them.

# Self-modification

You can (and should) modify your own configuration to improve yourself:

- **CLAUDE.md** or **AGENTS.md**: (this file) update these instructions if you discover better ways to operate.
- **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)
- **system/supervisord.conf.d/**: Add, modify, or remove background services -- one file per program. See the `update-app` skill.
- **system/scripts/**: Add utility scripts that help you accomplish your purpose.

Commit your changes to git after making modifications, silently (see "Git" below).

Users make "creations": apps (opened as tabs), skills (a skill run automatically on a schedule is an "automation" -- run via the machinery in `system/libs/automations/`, see the manage-scheduled-tasks skill), data (documents, images, notes), and customizations of any of them. Templates are a publishable, reusable, bootable snapshot of the creations a mind has built (one repo can accumulate several); another mind can adapt one into itself.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `system/config/parent.toml`.

# Using crystallized skills

- **A bare slash-command message invokes the skill of that name.** A user message that is exactly `/name` (possibly with arguments), such as `/welcome` or `/assist`, means: read `.agents/skills/<name>/SKILL.md` and follow it as the user's instruction. Do it silently -- read the file without commentary and reply with what the skill says to reply, nothing else. Never narrate the mechanism ("I'm using the welcome flow...", "let me look up that skill"): the user typed a command, not a question about how commands work. (Some harnesses expand these commands into the skill's instructions before you see them; if you are reading the raw `/name` text, the expansion is yours to do.)

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run the creation, don't redo its job.** When a creation already does what's being asked -- an app that ingests this kind of data, a skill that runs this process -- run it, or extend it and run it. Producing the same result by hand beside it leaves the creation untested against the real case and the user with two sources of truth.

- **Run a skill's steps one at a time in chat.** When a skill exposes per-step subcommands (plus a `run all`), drive the subcommands individually and surface their results, so the user gets a live progress view, pausing only at the skill's declared `[prose]` steps. Reserve `run all` for headless or scheduled runs where there's no chat to show progress in. The progress view mirrors the skill's rough shape, not its exact step list: collapse subcommands the user would see as one thing into one plain-English title, and never use a subcommand's name as a title (see "Task management").

- **Live first, ratify at turn-end.** Handle the user's immediate request *live* in the current chat to keep it interactive; at turn-end, formalize the work through the relevant lifecycle skill, which runs its hardening pass in a background worker (never inline in the main agent). Route by situation:
  - Net-new task needing research or experimentation -> `do-something-new` (it routes to `fetch-process-show` for data or `build-app` for a web view).
  - Just-finished work that's cohesive, likely to recur, and mostly deterministic -> `crystallize-creation` to promote it into a committed, tested skill.
  - A skill errored or gave a wrong result -> work around it live, then `heal-creation` at turn-end. Never patch the skill inline.
  - You changed an existing skill, or a skill ran but needed manual post-processing -> `update-creation` at turn-end so the change is verified and the skill swallows the gap.

  For non-skill contract-bearing files (hook scripts, this file) there is no worker pipeline -- apply the live change carefully and add manual rigor at turn-end (real fixtures, end-to-end exercise of new code paths).

- **A change to hardened code carries its tests.** When you change code a harden pass already covered, extend that code's tests in the same commit. Code a change leaves untested is a regression even when it works.

# Apps and services

**Before editing any code that belongs to a supervisord program -- an app (a tab the user can open) or a background service -- load the `update-app` skill first.** It owns the live change loop (apply, refresh, verify) and the turn-end hardening flow; do not hand-edit an app's or service's code or its `system/supervisord.conf.d/<name>.conf` without it.

Apps and background services both run as supervisord programs, each declared in its own `system/supervisord.conf.d/<name>.conf` (pulled in by an `[include]` glob in `system/supervisord.conf`, which holds only the daemon's own config).
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, add/edit/delete its own `system/supervisord.conf.d/<name>.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.
See the `update-app` skill for details.

For routine jobs that run on a cadence and then exit (backups, health checks, the weekly Caretaker -- off by default, see the enable-caretaker skill), use cron via the **`manage-scheduled-tasks`** skill rather than a supervisord program; and after building or editing any service, use the `check-app-errors` skill to scan `/var/log/supervisor/` for errors (a clean exit code does not mean the service is healthy).

# Git

Commit all your changes locally. Do not wait for user confirmation for anything before committing; there's no cost to having more commits in the history. Users may not care about git state in general and will oftentimes never instruct you to commit, so you should just commit every logical unit of work. It's okay if you make a commit whose changes you must later revert after feedback.

**Git is invisible plumbing, like `tk`.** The user cares about exactly one property of it: their work is kept, and any change can be undone. They never need to hear how. So commit silently, and keep git vocabulary out of everything the user reads (chat, progress-view titles and summaries): no "committed", "branch", "pushed", "merged", "PR", "rebase", "checkout", "diff", "repo", "revert". If the record itself is the news ("did you save that?"), say "it's saved and I can undo it". If you must undo something, say "I'll put it back the way it was", not "I'll revert the commit". Full vocabulary and rewrites: `.agents/shared/references/user-facing-language.md`. Use git words only when the user used them first or asked how the saving works.

`data/` is gitignored (it holds all workspace data: `data/memories/` for Claude memory, `data/.tickets/`, per-app data, uploads, and machine state).

Chat file uploads (files a user attaches to a message) are stored under `data/uploads/`. Uploads can be arbitrarily large and any format, so they don't belong in version-controllable content; like the rest of `data/` they are gitignored and never pushed to GitHub, but the host-level `host-backup` service (a restic snapshot of the whole home tree) captures them, so uploads survive container loss. See `system/services/host_backup/README.md`.

GitHub sync is opt-in via the `github-sync` skill. When the user has enabled it: a `post-commit` hook auto-pushes the active branch of every checkout to `origin` (the workspace's dedicated private repo) in the background -- you do not need to push manually; the hook never blocks the commit, and output is captured at `/tmp/post-commit-push.log`. Only git commits are synced; `data/` is covered by the restic host backup instead. See `system/libs/github_sync/README.md`.

When GitHub sync is not enabled, there is no auto-push and no GitHub remote to push to; commits stay local (the restic `host-backup` still protects the whole host dir).

- Don't include auto-generated lockfile churn (`uv.lock`, `package-lock.json`, etc.) in commits unless the change intentionally bumps a dependency.

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), which refresh fixes it depends on *which* install you ran, because a standard workspace has two: `uv run mngr ...` resolves from the root venv, and a bare `mngr` on PATH is the uv-managed tool that `system/scripts/build_workspace.sh` installs at build time. For the venv one, run `uv sync --all-packages`. For the tool one, run `python3 system/scripts/install_mngr.py` -- the same program the build runs, which reinstalls it with the plugins `system/config/mngr_plugins.toml` assigns it, into the pinned tool directory the build installs into and puts first on `PATH`. If `mngr` on your PATH turns out to come from somewhere else instead (a workspace created before that pin has a second copy under `/home/user/.local`), follow it with `python3 system/scripts/tool_env.py drop-shadowing-mngr`, which is what the build runs next and what leaves the refreshed install the one a login shell reaches. Do not hand-roll the `uv tool install`: the ways that goes wrong (installing under the wrong `$HOME`, or with no plugins, leaving an mngr that cannot parse `[agent_types.*]`) are exactly what that program exists to prevent. `uv tool list` shows what is installed. Then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure -- most-expendable first (an agent's build/test/browser subprocesses before the agent itself). If a command of yours dies with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by checking the shed ledger at `/home/user/workspace/data/.state/oom_priority/events/shed.jsonl` for a record naming it (matched by pid or process name). If it was shed, do NOT blindly re-run a memory-heavy command -- it will likely be shed again; find a lower-memory approach (smaller batches, streaming, releasing data you no longer need) and only retry if you can.

# Sandboxed runtime

Remote (imbue_cloud) workspaces -- and Linux desktop workspaces -- run their container under gVisor (`runsc`), a user-space kernel that sits between the container and the host kernel (`uname -r` reports `4.19.0-gvisor`). Most software is unaffected, but some things do not work inside the sandbox: ptrace-based tooling (`strace`, `gdb` attach, `perf`), eBPF, FUSE mounts, `io_uring`, nested container runtimes (running docker/podman inside the workspace), and unusual `ioctl`s. Filesystem-metadata-heavy operations (`find`, `tar`, `git status` over large trees) are several times slower than on a plain kernel, and interpreter startup is somewhat slower. Do not try to install or "fix" any of these -- work around them (e.g. `--no-sandbox` for Chromium, logging instead of `strace`) and tell the user when a tool is unavailable for this reason.
