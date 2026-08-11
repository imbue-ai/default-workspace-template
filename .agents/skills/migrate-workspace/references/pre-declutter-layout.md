# Migrating a pre-declutter workspace (`minds-v0.3.9` and earlier)

> **This document names retired terminology on purpose.** A migration map has to
> say what the old tree actually called things, or it cannot map them. The words
> below (`creations/`, "artifact", "web service", "application") are **retired**
> in this workspace's own vocabulary -- do not carry them into anything you write.
> The current vocabulary is: users make **creations** -- **apps** (opened as
> tabs), **skills** (an **automation** is a skill run on a schedule), **data**,
> and **customizations**; **service** means a background supervisord program with
> no tab.

Load this only when `detect-layout` reported `pre-declutter`. It covers the one
historic discontinuity between `minds-v0.3.9` and now: three tree
reorganizations (`mngr/fix-data-layout`, `mngr/declutter-template`,
`mngr/creation-rename`, landing through merge `9a08e250d` on 2026-07-26) plus a
container base-image change. This is a one-off for that discontinuity, not the
first of a versioned series.

## Why the test is the tree, not the version

The reorganization shipped in **no** `minds-v*` tag: `minds-v0.3.9` still carries
the old root layout. And a `minds-v0.3.8` workspace has **no version marker on
disk at all** -- the root `VERSION_HISTORY.md` only appeared between 0.3.8 and
0.3.9. So the source is identified by inspection (repo root at `/mngr/code`, a
`runtime/` directory, a root `supervisord.conf`, no `data/`), and the version
range above is context rather than the test.

## Absolute roots

The old image put the checkout and worktrees on a `/mngr` volume; the user-data
layout moved everything under one persistent `/home/user` tree. `/code` and
`/worktree` existed as safety-net symlinks and appear in older hardcoded
references, so map them too.

| Old | New |
|---|---|
| `/mngr/code` (also `/code`) | `/home/user/workspace` |
| `/mngr/worktree/` (also `/worktree/`) | `/home/user/worktrees/` |
| `/mngr` (mngr host dir: `agents/`, `preserved/`, `env`) | `/home/user/.mngr` |
| `/var/lib/minds/deferred-install/done.*` markers | gone -- see "Substrate deltas" |

`~/.cache` is now a symlink to `/var/cache/user` and `/tmp` is tmpfs, both
deliberately outside the backup. Nothing user-authored should be migrated into
either.

## Repo-relative paths

`migrate_workspace.py map-paths` applies this table, so use it rather than mapping
by hand; the table is here so you can check its answers and resolve the two rows
it flags **ambiguous**.

### State and data

| Old | New |
|---|---|
| `runtime/memory/` | `data/memories/` |
| `runtime/tickets/` | `data/.tickets/` |
| `runtime/harden/<flow>-<name>/` | `data/.tasks/<flow>/<name>/` |
| `runtime/secrets/` | `data/.secrets/` |
| `runtime/oom_priority/` | `data/.state/oom_priority/` |
| `runtime/backup.toml` | `data/system/backup.toml` |
| `runtime/applications.toml` | `data/.state/apps.toml` (now `[[apps]]` entries; `MINDS_APPS_FILE` overrides) |
| `runtime/<name>/` (per-app data, per-skill state, flow scratch) | **ambiguous**: `data/.apps/<name>/`, `data/.skills/<name>/`, or `data/.state/<name>/` |
| `uploads/` | `data/uploads/` |
| `github_sync.toml` (repo root) | `data/system/github_sync.toml` |

The `runtime/<name>/` row is ambiguous because the old tree used one directory for
four different kinds of thing. Read what is in it: an app's stored records go to
`data/.apps/<name>/`, a skill's own state to `data/.skills/<name>/`, machine state
to `data/.state/`, and a half-finished flow's scratch to `data/.tasks/`. The
convention now is "everything visible under `data/` is the user's to organize";
`data/documents/` and `data/my-project/` are the visible starter folders.

### Code and config

| Old | New |
|---|---|
| `supervisord.conf` | `system/supervisord.conf` (also symlinked at `/etc/supervisord.conf`) |
| `Dockerfile` | `system/Dockerfile` |
| `scripts/` | `system/scripts/` |
| `parent.toml` | `system/config/parent.toml` |
| `skills-lock.json` | `.agents/skills-lock.json` |
| `VERSION_HISTORY.md` | `docs/VERSION_HISTORY.md` |
| `style_guide.md` | `docs/system/style_guide.md` (a symlink into `system/vendor/mngr/`) |
| `blueprint/`, `specs/` | `docs/system/blueprint/`, `docs/system/specs/` |
| `changelog/`, `dev/changelog/` | `system/changelog/` |
| `test_meta_ratchets.py`, `test_mngr_template_stacking.py` | `system/test_meta_ratchets.py`, `system/test_mngr_template_stacking.py` |
| `vendor/mngr/`, `vendor/tk/` | `system/vendor/mngr/`, `system/vendor/tk/` |
| `apps/system_interface/` | `system/apps/system_interface/` |

The old flat `libs/` split three ways -- `system/apps/` for anything tab-openable,
`system/services/` for tab-less background daemons, `system/libs/` for support
libraries:

| Old | New |
|---|---|
| `libs/browser/` | `system/apps/browser/` |
| `libs/app_watcher/` | `system/services/app_watcher/` |
| `libs/cloudflare_tunnel/` | `system/services/cloudflare_tunnel/` |
| `libs/host_backup/` | `system/services/host_backup/` |
| `libs/oom_priority/` | `system/services/oom_priority/` |
| `libs/bootstrap/` | `system/libs/bootstrap/` |
| `libs/github_sync/` | `system/libs/github_sync/` |
| `libs/mngr_cli_contract/` | `system/libs/mngr_cli_contract/` |
| `libs/tk_command_parsing/` | `system/libs/tk_command_parsing/` |
| `libs/<user-built>/` | **ambiguous**: `system/apps/<pkg>/` if it serves a tab, `system/services/<pkg>/` if it is a tab-less daemon, `system/libs/<pkg>/` if it is a library |

The `libs/<user-built>/` row is the one that matters most in practice, since it is
where the user's own work lived. Decide it by what the package does: a
`forward_port.py` call in its supervisord block, or an HTTP server in its runner,
means it is an app. There is also a new `system/services/env_converge/`, with no
old counterpart. Top-level `apps` and `skills` symlinks point at `system/apps/`
and `.agents/skills/` for discoverability.

### The narrow in-between case

A workspace created from `main` *between* the declutter and the rename detects as
`current` layout (it has `system/`) but still carries the intermediate vocabulary.
If you see any of these, map them too:

| Intermediate | New |
|---|---|
| `creations/<pkg>/` | `system/apps/<pkg>/` (or `system/services/`, same judgement as above) |
| `data/creations/<name>/` | `data/.apps/<name>/` |
| `data/.state/applications.toml` | `data/.state/apps.toml` |
| `data/chat-files/`, `data/chat-images/` | `data/documents/` (shared files now live in visible homes and are served in place) |

## Substrate deltas

These are environment changes, not file moves. They matter because migrated code
often assumes the old substrate.

- **Base image.** `python:3.12.13-slim-bookworm` (Debian 12) ->
  `python:3.12-slim-trixie` (Debian 13). This is the change no in-place update can
  carry, and the reason migration exists. Any user code pinned to a bookworm
  package version needs re-checking.
- **apt is snapshot-pinned.** Every apt operation resolves against the archive
  frozen at the committed `.mngr/apt-snapshot-timestamp` (absent entirely before
  the cutover). No third-party apt repos remain: nodejs comes from trixie main and
  `gh` installs as a pinned sha256-verified binary. A migrated script that adds an
  apt repo or runs a bare `apt-get install` will not behave as it did -- move the
  install into a `system/scripts/env.d/` unit instead.
- **`deferred-install` became `env-converge`.** The old one-shot program left
  marker files under `/var/lib/minds/deferred-install/done.*`; the new one-shot
  `env-converge` program runs the `system/scripts/env.d/` units and re-installs
  anything the environment record has that the rootfs lacks, with **no marker
  files**. Readiness checks change accordingly: `supervisorctl status
  env-converge`, not `deferred-install`. `env-converge upgrade` is the one
  version-advancing operation, and `update-self` bundles it.
- **The browser engine is Fortress.** In `minds-v0.3.8` and earlier, Playwright
  drove its own managed Chromium (`chromium.launch()` with no
  `executable_path`). Now the engine is Fortress, a stealth-patched Chromium fork
  that Playwright's browser-cache lookup does **not** auto-discover: migrated
  automation must pass
  `executable_path="/opt/fortress/tilion-fortress/tilion"` explicitly, or the
  launch fails.
- **The uv workspace uses member globs.** `members` was an explicit list of every
  package; it is now `["system/libs/*", "system/services/*", "system/apps/*"]`, so
  a migrated app joins the workspace without a `members` edit -- it still needs its
  `[project].dependencies` + `[tool.uv.sources]` entries in the root
  `pyproject.toml`.
- **The `runtime-sync` branch is retired.** GitHub sync used to ship the whole
  `runtime/` tree to an orphan `runtime-sync` branch on the workspace's private
  repo. Now only git commits are synced, and workspace data under `data/` is
  covered by the restic `host-backup` instead. If the source has a `runtime-sync`
  branch, it is a **data source worth reading** (it may hold memories or state the
  live tree lost) but it is not something to reproduce here.
- **mngr's plain-text service logs** moved to `/var/log/mngr`; supervisord's
  per-program logs are at `/var/log/supervisor/<name>-{stdout,stderr}.log` in both.

## Skills and worker references that were renamed

A migrated skill or doc that names a left-hand entry will send an agent looking
for something that does not exist. `migrate_workspace.py audit-scan
--kind retired-skill` finds these; this table is the replacement.

| Old | New | Note |
|---|---|---|
| `build-web-service` | `build-app` | A tab-openable thing is an **app**, never a "web service" |
| `update-service` | `update-app` | Owns the live change loop for apps *and* background services |
| `crystallize-artifact` | `crystallize-creation` | The lifecycle hardens *creations*, parameterized by type |
| `update-artifact` | `update-creation` | |
| `heal-artifact` | `heal-creation` | |
| `.agents/shared/worker/references/harden-artifact.md` | `harden-creation.md` | |
| `.agents/shared/worker/references/artifact-skill.md` | `type-skill.md` | |
| `.agents/shared/worker/references/artifact-service.md` | `type-service.md` (plus a new `type-app.md`) | The old single reference split |
| `.agents/shared/worker/references/artifact-system-interface.md` | `type-system-interface.md` | |

The `launch-task` file-staging frontmatter key `source_artifacts_dir` kept its
name; it is not part of the rename.

## Capabilities that did not exist then

Worth telling the user about once the migration lands -- these are things their old
workspace simply could not do, and some of them are better homes for what they
built.

- **Scheduled tasks.** Recurring jobs run through cron drop-ins with a
  catch-up-and-retry runner (`system/scripts/run_job.sh`), and a **schedule agent**
  can run any skill on a cadence in its own chat tab. See the
  `manage-scheduled-tasks` skill. If the old workspace faked a schedule with a
  long-running loop in a supervisord program, this is where it should go instead.
- **The Caretaker.** A weekly maintenance agent, **off by default**, woken only
  when a deterministic check finds something (services in FATAL/BACKOFF, fresh
  errors in the supervisor logs, disk at 85 percent, new OOM shedding). See
  `enable-caretaker` / `disable-caretaker`.
- **Inspirations.** A publishable, bootable snapshot of what a mind has built, so
  another mind can be created from it or adopt it. See `publish-inspiration`,
  `use-inspiration`, `update-installed-inspiration`.
- **Layout operations.** `system/scripts/layout.py` inspects and rearranges the
  dockview tabs -- open, split, move, focus, rename, close, maximize, swap a URL.
  See `manage-layout`.
- **`data/.apps/` and `data/.skills/`.** Per-creation data has a declared home
  instead of sharing one `runtime/` directory, and the visible/hidden split under
  `data/` tells the user which folders are theirs to organize.
- **`check-app-errors`.** Scans `/var/log/supervisor/` for real errors -- a clean
  exit code does not mean a service is healthy.

## The 30-minute `host-backup-now` hang

Step 3 backs up the source over SSH. On a pre-declutter source that command can
sit for half an hour and then fail, **even though the backup tick it triggered
finished seconds in**.

The command waits by tailing the service's events log, and the version shipped
in `minds-v0.3.9` and earlier treats only `restic_backup_succeeded` /
`restic_backup_failed` as ending a tick. A tick can end without restic ever
running, and then neither event is ever written:

- `tick_skipped_due_to_missing_secrets` -- no `restic.env`, i.e. backups were
  never configured. **This is the likely case here**, and it is exactly the
  outcome Step 3 tells you to watch for. An old workspace that predates backup
  provisioning has no `restic.env` at all.
- `snapshot_failed` -- the snapshot step aborted the tick before restic.
- `tick_error` -- the loop's outer handler caught something.

In each case the tick is over, but the waiter polls to its 30-minute default
`--timeout` and exits 2 having printed nothing at all. The same blind spot
affects the in-flight wait that runs *first*, so a source whose last tick ended
in `snapshot_failed` looks permanently mid-backup.

So pass a short `--timeout`, and when it expires read the tick's real outcome off
the events log rather than believing the silence:

```bash
ssh ... 'cat "$MNGR_HOST_DIR/host_backup/service_events_dir"'
ssh ... 'tail -n 5 <that-dir>/events.jsonl'
```

That pointer file exists only on a source running the `minds-v0.3.9` backup
service or newer. Before that, the events sit under the *primary* agent's state
dir (`<host_dir>/agents/<primary-agent-id>/events/backup/events.jsonl` -- Step
5's `list-agents` names the primary), and `host-backup-now` over SSH fails
*immediately* rather than hanging, because it derives the events dir from
`MNGR_AGENT_STATE_DIR`, which an SSH session does not have. An immediate exit 2
saying it cannot locate the events log means an old backup service, not a broken
source.

Both of these are quirks of the command's *waiter*, never of the backup: the
service itself ticks, writes its events, and (given an `restic.env`) uploads
normally. Never read a `host-backup-now` timeout as "the source's backups are
broken" without checking the events log.

## Two gotchas specific to this crossing

- **The AI-integration helper moved *and* changed shape.** `claude_p.py` now lives
  at `.agents/skills/use-ai-integration/scripts/claude_p.py` (the
  `use-ai-integration` skill's prose still refers to it as
  `system/scripts/claude_p.py`; the path above is the real one). More importantly,
  credentials now resolve through `read_workspace_ai_credentials()` -- the
  `data/.secrets/anthropic.env` snapshot first, then the shared Claude settings,
  then the process env -- rather than from the process environment. A migrated call
  site that read `os.environ["ANTHROPIC_API_KEY"]` will silently find nothing,
  because services inherit a frozen env from supervisord.
- **Tickets moved with the data tree.** `TICKETS_DIR` now points at
  `data/.tickets/`. A migrated script that wrote `runtime/tickets/` directly will
  write to a directory nothing reads.
