# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE DURING YOUR WORK ON THIS PROJECT.

IF YOU FAIL TO FOLLOW ONE, YOU MUST EXPLICITLY CALL THAT OUT IN YOUR RESPONSE.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- If you ever need to work with another *git* repo that is *outside* of this monorepo as a read-only dependency, you should do so by adding a git subtree under `system/vendor/`.
- If you need to *actively develop* against an external repo, check out a standalone clone of it under `.external_worktrees/<repo-name>/`. This directory is gitignored so the external clones don't pollute the monorepo. The branch in the external clone should mirror the branch you're on in this monorepo. mngr is not one of them: it is installed here as packages from the commit `pyproject.toml` pins (`[tool.uv.sources]`; the public mirror for every release, or the private mngr-internal repo while a branch iterates on a paired change), so a change to it is described and submitted per `.agents/skills/submit-upstream-changes/references/mngr-changes.md` rather than developed from this workspace.
- All relative paths in this repo assume cwd = repo root (`/home/user/workspace`). Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. User-facing workspace data lives under `data/` (visible folders are the user's to organize; e.g. `data/.apps/<name>/` holds an app's stored data, including its instance records at `data/.apps/<name>/instances.json`, and `data/.skills/<name>/` a skill's own state); flow-internal scratch lives under `data/.tasks/<flow>/` and machine state (what a program keeps about this machine and can rebuild: the registry, dispatch scripts, pty records, the shell's client layouts) under `data/.state/`. The rule is `docs/system/blueprint/workspace-app-model/contracts.md` section 17.
- When adding a new app, use the `build-app` skill, which sets up a new package under `system/apps/` + a supervisord program entry + `forward_port.py` registration on its own port. Every app icon -- a new one or an edit to an existing one -- is built to `docs/system/app-icons.md`: a 216x216 two-layer tile, one fill-only glyph in a centred 144x144 box, coloured from the pair palette that doc carries. Do NOT edit `system/apps/system_interface/` for this -- that's the top-level workspace UI, not a template for new apps.
- A user can right-click anything on their screen and attach a description of it to a message as a `REF-<id>.json` file ("Explain what I attached in REF-..."). `.agents/shared/references/element-references.md` says what the file holds and how to find the element it names.

# Continuing a chat that moved to you

- When `MINDS_CHAT_ID` is set and differs from `MNGR_AGENT_ID`, this chat ran on another agent before you, and your first message says what to do. If it does not, `mngr list --include 'labels.chat_id == "$MINDS_CHAT_ID"'` lists your predecessors and `mngr transcript <agent-id>` reads their transcripts. Never touch a predecessor, and do not mention the switch to the user unless they ask.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- To test an app you built in this workspace, or to script a check of a public page that needs no account, use **Playwright's Python API** in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`). Anything else in a browser -- the user's logins, something the user should watch or help with, "log into my account and..." -- goes through the `connect-external-service` skill (see Communication below).
- Playwright here drives Fortress (a stealth-patched Chromium fork), not Playwright's own managed Chromium: pass `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly to `chromium.launch(...)`, since Playwright's browser-cache lookup only auto-discovers builds it downloaded itself. Fortress installs asynchronously on first container boot (the one-shot `env-converge` program's env.d units), so in a fresh workspace confirm it finished -- `supervisorctl status env-converge` or `test -x /opt/fortress/tilion-fortress/tilion` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `args=["--no-sandbox"]`. See `system/libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background.
- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Before finishing your response, reflect on your work and identify any potential issues. Tell the user only the ones that affect them, in plain language ("the report skips entries with no date; want me to include those?"); the rest stay in your own thinking.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final message, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), offer it as a one-line follow-up in the user's terms.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / status line / web) -- just propose the web view and only deviate when there's a specific reason (e.g. CLI for batch jobs).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records. *Preserve*: durably persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever gets back to the origin) -- not just in memory for the current run; don't fetch-transform-discard, so a later change in processing needs no refetch. *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask -- so they can bridge any gap the derived view leaves. **Render the raw record in its native format** (HTML email as the rendered email, JSON pretty-printed, markdown rendered -- not escaped source text); "raw" means *unprocessed by your derivation*, not *unrendered*. Build these affordances in by default but **keep them subtle** -- don't announce in chat that you're saving data or adding a "view raw" control.
- **Naming is informative, not cheeky.** Service names, app names, skill names, command names: prefer something that explains what the thing does (`slack-inbox-checker`) over something clever (`nothing-new`). Cute names tax every later mention.
- **Platform-internal APIs are valid.** Don't restrict yourself to officially documented public APIs. If a platform's own client (web app, mobile app) uses internal or undocumented endpoints to do something, those endpoints are fair game -- inspect what the official client actually calls and use the same endpoints with the same user-session auth. This is often cleaner than designing brute-force workarounds on top of a limited public API.

# Checking your work

Checking what you built before replying (a `curl` of its port, `python3 system/scripts/smoketest_app.py <name>`, a Playwright pass) is optional; use your judgment.

# Communication

If the user talks to you about files or directories on disk, assume (unless context indicates otherwise) they mean their local disk, not the one in your sandbox -- use the `file-sharing` skill to bridge the two.

When a chat reply mentions a workspace file, write its path in code formatting (`data/reports/q4.md`), not as a markdown link. The chat renders only web links and absolute-path download links (the `show-files-in-chat` skill); any other path link shows as plain text. If the user should look at a file under `data/`, suggest they open it in the File Viewer (it opens at the workspace folder and can browse the whole container).

If the user asks you to read or act on anything outside this workspace on their behalf -- a third-party tool they have an account with, a link they paste (a Notion page, Google Doc, or Slack thread), a site to sign in to, or a browser they want to watch -- load the `connect-external-service` skill before running any command against it. The skill decides how to reach the service and what to ask the user for; do not pick a method yourself first. The one exception is reading a public page that needs no account, which your web tools can do directly.

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

Users make "creations": apps (opened as windows), skills (a skill run automatically on a schedule is an "automation" -- run via the machinery in `system/libs/automations/`, see the manage-scheduled-tasks skill), data (documents, images, notes), and customizations of any of them. Templates are a publishable, reusable, bootable snapshot of the creations an agent has built (one repo can accumulate several); another agent can adapt one into itself.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `system/config/parent.toml`.

**Finding a defect in built-in code is itself a reason to escalate it upstream -- the user does not have to ask.** Built-in means mngr (installed from the commit `pyproject.toml` pins), vendored (`system/vendor/`), from the initial template commit, or arrived via an `update-self:` merge; the `/assist` skill's "Classify the cause" section has the exact test. The code that needs changing is upstream's, so a local ticket cannot reach it: every workspace that hits the same bug would rediscover it and bury it again. Two channels, by what you have:

- A diagnosis, no fix: report it, using the POST in `.agents/shared/references/report-built-in-issues.md`. It pops a modal for the user to review and send, so the human still gates it. One report per pass, covering everything you found -- not one per issue.
- A fix you can stand behind: `submit-upstream-changes`, which opens a PR against the parent template repo. Not for mngr: a fix there is its own PR on the mngr repo, not a template one, so an mngr defect goes in a report.

Fixing it locally is *not* an alternative: a fix to a file that is byte-identical to the release only manufactures divergence the next update has to reconcile. Either escalate it, or tell the user plainly that you found one and are not escalating it, and why.

# Using crystallized skills

- **A bare slash-command message invokes the skill of that name.** A user message that is exactly `/name` (possibly with arguments), such as `/assist`, means: read `.agents/skills/<name>/SKILL.md` and follow it as the user's instruction. Do it silently -- read the file without commentary and reply with what the skill says to reply, nothing else. Never narrate the mechanism ("I'm using the assist flow...", "let me look up that skill"): the user typed a command, not a question about how commands work. (Some harnesses expand these commands into the skill's instructions before you see them; if you are reading the raw `/name` text, the expansion is yours to do.)

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run the creation, don't redo its job.** When a creation already does what's being asked -- an app that ingests this kind of data, a skill that runs this process -- run it, or extend it and run it. Producing the same result by hand beside it leaves the creation untested against the real case and the user with two sources of truth.

# Apps and services

**Before editing any code that belongs to a supervisord program -- an app (a window the user can open) or a background service -- load the `update-app` skill first.** It owns the change mechanics (apply, restart); do not hand-edit an app's or service's code or its `system/supervisord.conf.d/<name>.conf` without it. It reads the app's `app.toml` first: a critical app (the shell, the chat, the terminal, or any app that declares `critical = true`) and the shared `system/libs/workspace_ui/` and `system/libs/workspace_layout/` libraries take its careful flow, which never edits the served tree.

Apps and background services both run as supervisord programs, each declared in its own `system/supervisord.conf.d/<name>.conf` (pulled in by an `[include]` glob in `system/supervisord.conf`, which holds only the daemon's own config).
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, add/edit/delete its own `system/supervisord.conf.d/<name>.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.
See the `update-app` skill for details.

For routine jobs that run on a cadence and then exit (backups, health checks, the weekly Caretaker -- off by default, see the enable-caretaker skill), use cron via the **`manage-scheduled-tasks`** skill rather than a supervisord program. The `check-app-errors` skill scans `/var/log/supervisor/` for errors when a service misbehaves (a clean exit code does not mean the service is healthy).

# Git

Keep git vocabulary (commit, branch, push, merge, PR, rebase, diff, repo) out of what the user reads unless they used it first; if they ask whether something is saved, say "it's saved and I can undo it".

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

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure -- most-expendable first (an agent's build/test/browser subprocesses before the agent itself). If a command of yours dies with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by checking the shed ledger at `/home/user/workspace/data/.state/oom_priority/events/shed.jsonl` for a record naming it (matched by pid or process name). If it was shed, do NOT blindly re-run a memory-heavy command -- it will likely be shed again; find a lower-memory approach (smaller batches, streaming, releasing data you no longer need) and only retry if you can. When an agent reports a shed to you, free memory with the user per `.agents/shared/references/freeing-memory.md`.

A backup that takes minutes (a slow `host-backup-now`, or a `BACKUP_SLOW` event in the backup events log) is caused by how many files the home tree holds, not how big they are. Follow "Slow backups" in `system/services/host_backup/README.md`, which uses `uv run host-backup-heavy-dirs` to find the directories responsible.

`/tmp` is a small RAM disk: a tmpfs capped at about 1 GiB, whose contents count against the container's memory limit until deleted. A write past the cap fails with "No space left on device" (ENOSPC), and until you delete what filled it, everything else that writes to `/tmp` fails the same way. Keep it to small files. Anything that can run to gigabytes -- a copy of an app's data, a clone, a download, a backup export or restore -- goes on disk: a throwaway file under `/var/tmp` (outside the backup), and a copy of an app's data through `serve_isolated_instance.py --copy` (see the `update-app` skill), which checks that it fits.

# Sandboxed runtime

Some providers run the workspace container under gVisor (`runsc`), a user-space kernel that sits between the container and the host kernel. Check with `dmesg 2>/dev/null | grep -q 'Starting gVisor'` (`uname -r` also reports `4.19.0-gvisor`). Most software is unaffected, but some things do not work inside the sandbox: ptrace-based tooling (`strace`, `gdb` attach, `perf`), eBPF, FUSE mounts, `io_uring`, nested container runtimes (running docker/podman inside the workspace), and unusual `ioctl`s. Filesystem-metadata-heavy operations (`find`, `tar`, `git status` over large trees) are several times slower than on a plain kernel, and interpreter startup is somewhat slower. Do not try to install or "fix" any of these -- work around them (e.g. `--no-sandbox` for Chromium, logging instead of `strace`) and tell the user when a tool is unavailable for this reason.
