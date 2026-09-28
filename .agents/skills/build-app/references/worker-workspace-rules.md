# Workspace rules for a worker

**This file is how the workspace works, for you.** Your worktree's `CLAUDE.md`
imports it, so it is loaded automatically and it is the whole of what you are
told about this workspace. The repo's own `AGENTS.md` is not loaded for you: it
is written for an agent that holds a chat with the user and owns a whole task,
and you are a node of a build, with one subtask, one report and no chat. What
follows is taken from it, with the parts meant for the other kind of agent left
out. If you open `AGENTS.md` yourself, prefer this file wherever they differ.

Your brief is your task file and the handoffs quoted inside it. Read those and
`.agents/skills/build-app/references/worker-node.md`, then read only as much of
the rest of the repo as your own subtask needs.

You launch no agents of your own.

These rules cover every kind of node a plan can name, so some of what follows is
not yours. If your subtask is to build a piece rather than to check one, skip
**Manual verification and testing** -- your check is the single command
`worker-node.md` names, and the suite, coverage, ratchets and the review gates
belong to the hardening pass at the end of the build. Read that section only if
your task file asks you to verify something or to write tests.

# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE AND IN YOUR TASK FILE.

IF YOU FAIL TO FOLLOW ONE, SAY SO EXPLICITLY IN YOUR REPORT.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- All relative paths in this repo assume cwd = the root of the checkout you are working in -- for you that is the build folder you were started in, not `/home/user/workspace`. Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. User-facing workspace data lives under `data/` (visible folders are the user's to organize; e.g. `data/.apps/<name>/` holds an app's stored data, including its instance records at `data/.apps/<name>/instances.json`, and `data/.skills/<name>/` a skill's own state); flow-internal scratch lives under `data/.tasks/<flow>/` and machine state (what a program keeps about this machine and can rebuild: the registry, dispatch scripts, pty records, the shell's client layouts) under `data/.state/`. The rule is `docs/system/blueprint/workspace-app-model/contracts.md` section 17.

# Where the data is

`data/` is gitignored, so your worktree carries almost none of it. The workspace's
own data is at `/home/user/workspace/data/`.

Read data from there if you need to, and write any data changes there too.

# Progress tracking is not yours

You do not use `tk`. The orchestrating agent that launched you keeps the one progress timeline
the user sees, and it records the build's stages there on your behalf.

Your report file is how your work is seen. Write it as your task file says, and let that be the
whole account of what you did.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- Drive a browser with **Playwright's Python API** in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`): for testing an app you just built, scraping a page into a file, or a one-off check.
- The browser here is Fortress (a stealth-patched Chromium fork), not Playwright's own managed Chromium. For the Python API, pass `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly to `chromium.launch(...)`, since Playwright's browser-cache lookup only auto-discovers builds it downloaded itself. Fortress installs asynchronously on first container boot (the one-shot `env-converge` program's env.d units), so in a fresh workspace confirm it finished -- `supervisorctl status env-converge` or `test -x /opt/fortress/tilion-fortress/tilion` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `args=["--no-sandbox"]`. See `system/libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / status line / web) -- just propose the web view and only deviate when there's a specific reason (e.g. CLI for batch jobs).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records. *Preserve*: durably persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever gets back to the origin) -- not just in memory for the current run; don't fetch-transform-discard, so a later change in processing needs no refetch. *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask -- so they can bridge any gap the derived view leaves. **Render the raw record in its native format** (HTML email as the rendered email, JSON pretty-printed, markdown rendered -- not escaped source text); "raw" means *unprocessed by your derivation*, not *unrendered*. Build these affordances in by default but **keep them subtle** -- don't announce in chat that you're saving data or adding a "view raw" control.
- **Naming is informative, not cheeky.** Service names, app names, skill names, command names: prefer something that explains what the thing does (`slack-inbox-checker`) over something clever (`nothing-new`). Cute names tax every later mention.
- **Platform-internal APIs are valid.** Don't restrict yourself to officially documented public APIs. If a platform's own client (web app, mobile app) uses internal or undocumented endpoints to do something, those endpoints are fair game -- inspect what the official client actually calls and use the same endpoints with the same user-session auth. This is often cleaner than designing brute-force workarounds on top of a limited public API.

# Manual verification and testing

Before declaring any feature complete, manually verify it: exercise the feature exactly as a real user would, with real inputs, and critically evaluate whether it *actually does the right thing*. 
Do not confuse "no errors" with "correct behavior" -- a command that exits 0 but produces wrong output is not working.

Then crystallize the verified behavior into formal tests. 
Assert on things that are true if and only if the feature worked correctly -- this ensures tests are both reliable and meaningful.

# Using crystallized skills

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run the creation, don't redo its job.** When a creation already does what's being asked -- an app that ingests this kind of data, a skill that runs this process -- run it, or extend it and run it. Producing the same result by hand beside it leaves the creation untested against the real case and the user with two sources of truth.

- **Live first, ratify at turn-end.** Work is done first and formalized afterwards, through the relevant lifecycle skill, which runs its hardening pass in a background worker (never inline). Route by situation:
  - Net-new task needing research or experimentation -> `do-something-new` (it routes to `fetch-process-show` for data or `build-app` for a web view).
  - Just-finished work that's cohesive, likely to recur, and mostly deterministic -> `crystallize-creation` to promote it into a committed, tested skill.
  - A skill errored or gave a wrong result -> work around it live, then `heal-creation` at turn-end. Never patch the skill inline.
  - You changed an existing skill, or a skill ran but needed manual post-processing -> `update-creation` at turn-end so the change is verified and the skill swallows the gap.

  For non-skill contract-bearing files (hook scripts, this file) there is no worker pipeline -- apply the live change carefully and add manual rigor at turn-end (real fixtures, end-to-end exercise of new code paths).

  In a build you are a node of, the turn-end handoff is not yours: after the
  last node the orchestrating agent makes the single `crystallize-creation`
  call for the whole app. Report what you built and stop.

- **A change to hardened code carries its tests.** When you change code a harden pass already covered, extend that code's tests in the same commit. Code a change leaves untested is a regression even when it works.
  (Only where your subtask asks you to touch that code.)

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
