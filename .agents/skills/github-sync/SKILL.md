---
name: github-sync
description: Enable, check, or disable GitHub sync for this workspace, and the private repository each app can have of its own. Enabling creates a dedicated PRIVATE GitHub repo via latchkey, points origin at it, auto-pushes every commit from every checkout, and gives each app built here a private dev repository (<name>-dev) that is kept current. Workspace data under data/ is NOT synced to GitHub (the restic host backup covers it). Use when the user asks to back up / sync the workspace to GitHub, enable auto-push, asks about GitHub sync status, or wants one app in a private repository of its own or to give someone access to an app without publishing it.
compatibility: Requires latchkey (see the connect-external-service skill) and the user approving GitHub permissions in the Imbue Studio app.
metadata:
  author: imbue
---

# GitHub sync

GitHub sync is opt-in. Nothing syncs until this skill enables it. Once
enabled, three pieces work together (see `system/libs/github_sync/README.md`):

1. `origin` points at a dedicated **private** GitHub repo for this workspace.
2. Global git wiring routes all `https://github.com/...` traffic through the
   latchkey gateway (credential injected server-side; no token in the
   container) and activates the `post-commit` hook, so every commit on any
   checkout -- main repo and worker worktrees -- auto-pushes its branch.
3. The `[program:github-sync]` service is a watchdog: every 60s it re-applies
   the git wiring (self-healing gateway port changes) and re-verifies the repo
   stays private, halting the hook's pushes if it ever isn't. The same pass
   keeps each app's own private repository current (see "App dev
   repositories" below).

What is synced is exactly what is committed to git. Workspace data under
`data/` (memories, tickets, uploads, per-app data) is gitignored and is
NOT shipped to GitHub -- the restic `host-backup` service covers it.

## Hard rules

- **Private repos only.** Never create a public repo, never point sync at a
  public repo, and never work around a visibility halt. If the user asks for
  a public sync repo, decline and explain: agents can push secrets or other
  sensitive data without realizing it.
- **Everything flows through latchkey.** Never ask the user for a GitHub
  token and never embed credentials in URLs or git config.
- `origin` is reserved for the sync repo. Upstream-template operations keep
  using `system/config/parent.toml` (see the update-self skill) and are unaffected.
- **A dev repository is private, and it is not a way to publish.** Making an
  app public is the `publish-app` skill, which scans and filters what it
  shares. Never make a dev repository public in its place.

## Enable

1. **Check current state**: `uv run --no-sync github-sync status`. If `is_configured`
   is already true, jump to "Status" (or "Repair" if the service is
   unhealthy). Also run `supervisorctl status github-sync` (it errors when no
   such program exists -- expected before enable).

2. **Request GitHub permissions** through latchkey (see
   `.agents/skills/connect-external-service/references/latchkey.md` for the
   permission-request mechanics). GitHub exposes two latchkey scopes
   and a permission request carries exactly one scope, so this is two
   requests. A request must be the **only** command in its tool call, so these
   are two calls -- send them back to back; you do not have to wait for the
   first verdict to file the second.
   Do both **before any other GitHub call**, and say up front that two
   approvals are coming so the user is not surprised by the second. Never
   dribble out further requests later in the flow.

   First call, on its own:

   ```bash
   latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
     -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
     -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-read", "github-git-write"]}, "rationale": "GitHub sync: push this workspace'"'"'s branches to your private sync repo."}'
   ```

   Then the second call, on its own:

   ```bash
   latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
     -H 'Content-Type: application/json' -H 'X-Latchkey-Device: *' \
     -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-read-repos", "github-write-all"]}, "rationale": "GitHub sync: create the private sync repo (needs github-write-all), confirm which GitHub account it lands under (github-read-user), and verify it stays private (github-read-repos)."}'
   ```

   This exact permission set is what the flow needs -- do not trim it, or the
   user gets asked again mid-flow:

   - `github-write-all` -- repo creation (`POST /user/repos`). The narrower
     `github-write-repos` covers only existing-repo (`/repos/{owner}/{repo}`)
     paths and is **not** enough to create a repo. It also covers the
     optional repo deletion on disable.
   - `github-read-repos` -- the recurring private-visibility check
     (`GET /repos/{owner}/{repo}`), which the service repeats forever.
   - `github-read-user` -- `GET /user`, to name the account the repo will be
     created under (step 3) and as the one legitimate "did the grants land?"
     probe (below).
   - `github-git-read` / `github-git-write` -- clone/fetch and push.

   Then **wait for both approval system messages** ("Your permission request
   for GitHub (git) / (REST API) was granted..."). Those messages are the
   authoritative signal; they are what tells you to proceed.

   **Do not treat a rejected API call as evidence that a grant is missing.**
   `{"error": "Error: Request not permitted by the user."}` means *that
   endpoint* is not covered by the granted permissions -- it does not mean
   the approval failed to arrive. Probing an endpoint outside the set above
   (e.g. `GET /user/repos`, which needs broader read access) will be rejected
   even when everything is granted correctly. If you want to sanity-check
   after the grant messages arrive, the only probe to use is:

   ```bash
   latchkey curl -s https://api.github.com/user
   ```

   Never re-ask the user for a permission you have already been told was
   granted; re-read this list and check *which endpoint* you called instead.

3. **Pick the repo.** Default: create a brand-new private repo named after
   the workspace (`$MINDS_WORKSPACE_NAME`), owned by the authenticated GitHub
   account (read its `login` from `latchkey curl -s https://api.github.com/user`).
   Confirm the name and owner with the user first; they can name an org
   instead. One exception: if `git remote get-url origin` already points at a
   user-owned repo (not `imbue-ai/default-workspace-template` or another
   shared template), ask whether to reuse it or create a fresh one --
   recommend a fresh dedicated repo unless they have a specific reason.
   Reused repos must be verified private and writable like new ones.

4. **Create the repo** (skip if reusing):

   ```bash
   latchkey curl -s -X POST https://api.github.com/user/repos \
     -H 'Content-Type: application/json' \
     -d '{"name": "<repo-name>", "private": true, "description": "Private sync repo for the <workspace> Imbue Studio workspace"}'
   ```

   (For an org: `POST https://api.github.com/orgs/<org>/repos`.) On a 422
   name-taken error, append `-2`, `-3`, ... and retry. The response JSON must
   contain `"private": true` -- if it does not, delete/abandon the repo and
   stop; do not proceed with a public repo. The response's `full_name` is the
   authoritative `<owner>/<repo>` to use from here on.

5. **Point origin at it and record the config**:

   ```bash
   git remote set-url origin https://github.com/<owner>/<repo>.git \
     || git remote add origin https://github.com/<owner>/<repo>.git
   ```

   Write `data/system/github_sync.toml` (this file is the "sync is
   enabled" marker for the service and the post-commit hook; it lives under
   the gitignored `data/system/` with the other runtime-written config):

   ```toml
   # Written by the github-sync skill. Presence of this file enables GitHub
   # sync; `uv run --no-sync github-sync status` reports on it.
   repo_url = "https://github.com/<owner>/<repo>"
   ```

6. **Wire git through the gateway**: `uv run --no-sync github-sync wire-git`. From now
   on plain `git push`/`git fetch` against github.com works in every
   checkout, and the post-commit auto-push hook is active.

7. **Verify private before any push**: `uv run --no-sync github-sync check-visibility`
   must print `private` (exit 0). If not, stop and surface the problem.

8. **Initial sync**: push the current branch and any existing worker
   branches:

   ```bash
   git push --set-upstream origin "$(git branch --show-current)"
   for b in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v -x -e "$(git branch --show-current)"); do git push origin "$b"; done
   ```

9. **Add the service** by writing this block to its own
    `system/supervisord.conf.d/github-sync.conf`, then
    `supervisorctl reread && supervisorctl update` (see the update-app
    skill). Its own file, not an append to the shared config, so enabling sync
    never collides with an app being scaffolded at the same time:

    ```ini
    # Opt-in GitHub sync (added by the github-sync skill): keeps the gateway
    # git wiring fresh and re-verifies the sync repo stays private (the
    # post-commit hook does the pushing). See system/libs/github_sync/README.md.
    # The oom_tag_service.py prefix sets its OOM shed-priority band (see
    # system/services/oom_priority).
    [program:github-sync]
    command=python3 system/services/oom_priority/bin/oom_tag_service.py github-sync bash -c "uv sync --all-packages --frozen && exec .venv/bin/github-sync run"
    directory=/home/user/workspace
    autostart=true
    autorestart=true
    startretries=1000000
    stopasgroup=true
    killasgroup=true
    stdout_logfile=/var/log/supervisor/github-sync-stdout.log
    stderr_logfile=/var/log/supervisor/github-sync-stderr.log
    stdout_logfile_maxbytes=10MB
    stderr_logfile_maxbytes=10MB
    stdout_logfile_backups=3
    stderr_logfile_backups=3
    ```

10. **Commit the enablement** (`system/supervisord.conf.d/github-sync.conf`; the
    config file is gitignored under `data/system/`). The now-active hook pushes
    the commit.

11. **Give each app its dev repository.** `uv run --no-sync app-install list`
    names every app built or installed here. For each one whose `state` is
    `owned` (built here) or `modified` (installed, then changed here), run,
    from the repo root of the live workspace and never from a worker's
    worktree:

    ```bash
    uv run --no-sync app-install link <name>
    ```

    It creates the private repository `<name>-dev` under the user's own
    GitHub account (theirs even when the sync repo sits under an
    organization), records its URL in the app's record, and commits that one
    file; the service then fills the repository on its next pass and keeps
    it current. Skip an app whose `state` is `installed`: one that is still
    as it came has no dev repository, and `link` refuses it. Read each result
    as "App dev repositories" below says, and carry on past an app whose
    repository could not be created; the workspace's own sync does not depend
    on it.

12. **Report**, in plain language (`.agents/shared/references/user-facing-language.md`):
    where the copy lives (the repo URL, since they may want to open it), that
    from now on every change you save is copied there automatically, that each
    app they built also has a private copy of its own (name them, with their
    URLs) and what a private copy holds if they ever share one ("Giving
    someone access to a dev repository" below), that the files in their
    workspace folders (uploads, notes, app data) are protected by a separate
    backup rather than this one, and that if the connection drops the copy
    catches up on its own. No commits, pushes, hooks, or gateways in the
    message.

## App dev repositories

An app this workspace built or installed (one with a record under
`system/config/apps/`) lives in the workspace's repository, at
`system/apps/<package>/`. It can also have a repository of its own, its **dev
repository**:

- It is named `<name>-dev`, it is always **private**, and it holds the app's
  directory at its root with the app's own history: every workspace change
  that touched the app, as a change of the app's.
- It is written automatically. Nothing is pushed to it by hand, and nothing
  that lands in it comes back into the workspace.
- It is for the user and the people they choose to read and work on the app
  as a small repository of its own. It is not what other people install from:
  that is the app's published repository, which exists only once the user
  publishes the app (the `publish-app` skill).

Two things keep it going, and they are separate on purpose:

- **`app-install link <name>`**, which an agent runs, does what must happen
  once: it gives the app's history a starting point in the workspace (so
  later passes take a fraction of a second rather than minutes) and creates
  the repository. Its result is `linked`, `refreshed` or `already_linked`,
  with `dev_repo_url`, `is_dev_repository_created`, `remaining` (what is left
  before the app is synced, in sentences) and `problems` (what it tried and
  could not do). A repository that could not be created, because the account
  already has a `<name>-dev` or a GitHub permission is missing, does not undo
  the rest: fix the cause and run `link` again. `refused` changed nothing;
  `detail` says why (run outside the live workspace's root, an app with no
  saved change yet, an installed app nobody has changed).
- **The service** then compares the app's directory with what it last pushed,
  on every pass, and pushes what is new. It never creates a repository, never
  commits to the workspace, and never forces a push.

Which apps have one:

- **An app built here** gets one when sync is enabled (step 11), and a new
  app gets one when it is built while sync is on (the `build-app` skill runs
  `link` after the app's first commit).
- **An installed app** has none while it is as it came: it follows the
  repository it was installed from. Once this workspace changes it (`state`
  is `modified` in `app-install list`), `app-install link <name>` gives it one
  the same way, and its history there continues from the publisher's.

### One app, without syncing the workspace

When the user wants one app in a private repository of its own and has not
enabled GitHub sync, file the two permission requests of Enable step 2
(saying they are for this app's repository), then:

```bash
uv run --no-sync app-install link <name> --create-dev-repo
uv run --no-sync github-sync sync-app <name>
```

The first creates the repository; the second pushes the app once and prints
one JSON line whose `result` is one of the states below (it exits nonzero
unless that is `pushed` or `up_to_date`). Nothing keeps that repository
current afterwards: run `sync-app` again after later changes, and tell the
user that this copy is as of the last time it was brought up to date.

### Giving someone access to a dev repository

A user who wants particular people to see or work on an app without
publishing it can add them to its dev repository, in that repository's
settings on GitHub. Before they do, tell them plainly what those people will
see, because it is more than the app as it is today:

- **Nothing has scanned it for secrets.** The scan that guards a published
  app has never run on a dev repository. A password, key or token that was
  ever saved in one of the app's files is in its history, even if it was
  removed later.
- **It carries the message of every workspace change that touched the app**,
  as it was written. A change that also touched something else is described
  in full, so a message can mention the user's other apps or other work.
- **It is the whole history**, not one clean version: every intermediate
  state of the app is there to read.

To share an app with people who should see none of that, publish it instead
(`publish-app`).

### What the status says about each app

`uv run --no-sync github-sync status` lists every app under `service.apps`,
each with a `state`, its `dev_repo_url`, `visibility`, `pushed_commit` and a
`detail` saying why. `service.app_pass_error` is set when the pass did not
run at all.

| `state` | Meaning | What to do |
| --- | --- | --- |
| `pushed` | The app's new changes were pushed on this pass. | Nothing. |
| `up_to_date` | The repository already holds the app as it is. | Nothing. |
| `unmodified` | An installed app nobody has changed; it has no dev repository by design. | Nothing. |
| `no_dev_repository` | An app built here, or a changed installed app, whose record names no repository yet. | Run `uv run --no-sync app-install link <name>`. |
| `not_linked` | The app has a repository but its history has no starting point, so the pass skips it rather than spend minutes on it. | Run `uv run --no-sync app-install link <name>`, from the live workspace's root. |
| `visibility_unknown` | The repository has not been confirmed private yet, so pushes wait. It clears by itself once GitHub answers. | If it stays, check the GitHub permissions are still granted, and that the repository still exists. |
| `not_private` | The repository is public. Its pushes are stopped; the workspace's own sync and every other app carry on. | Tell the user it has become public and that anyone can read its whole history, and ask them to make it private again in its settings on GitHub. Pushes resume by themselves within about fifteen minutes. Never work around the stop. |
| `push_refused` | The repository holds history this workspace's copy of the app does not continue, as when a second workspace (one restored from a backup, say) pushes the same app to it. Nothing was overwritten, and nothing will be. | Tell the user two workspaces are writing to one copy, and ask which one should keep it. In the other, the copy stays as it is; do not try to force it. |
| `failed` | Anything else that stopped this app; `detail` has the reason. | Act on `detail`. The pass tries again each time. |

An app whose `failed` detail says its directory "was removed and made again"
cannot have a dev repository: its history would carry the workspace's own
changes and files along with the app's, so nothing is ever pushed for it. Say
so plainly if the user asks why that app has no copy; there is no command
that changes it.

A dev repository the user deleted on GitHub is never recreated behind their
back: its pushes fail and the status says so. If they ask for it back, create
an empty private repository of the same name under the same account (as
Enable step 4 does), delete `data/.state/app_sync/<name>.json` so the next
pass pushes everything again, and check the status.

## Status

`uv run --no-sync github-sync status` prints config + the service's latest status
(visibility, errors); `supervisorctl status github-sync` shows the
process; logs are at `/var/log/supervisor/github-sync-*.log` and
`/tmp/github-sync.log`, hook output at `/tmp/post-commit-push.log`. Explain
findings in plain language. If `is_push_allowed` is false, the repo is public
or unverifiable -- tell the user to make it private again; sync resumes
automatically. Each app's own repository is reported separately, under
`service.apps`: see "What the status says about each app" above. One app's
trouble never stops the workspace's sync or another app's.

## Repair (workspace recreated from a synced repo)

A workspace created from a previously-synced private repo inherits the
service block, but not the gitignored `data/system/github_sync.toml`, the
latchkey permissions, or the container-local wiring. To repair: re-write the
config file (step 5), then run step 2 (permission requests); the service
self-heals within a tick (re-wires git). Verify with "Status", or accelerate
with `uv run --no-sync github-sync wire-git`. Workspace data under `data/` comes back
via a restic backup restore, not via GitHub.

## Disable

Confirm with the user first, and ask separately whether to keep the remote
repo (recommend keeping it -- it costs nothing and preserves history).

1. `supervisorctl stop github-sync`, delete
   `system/supervisord.conf.d/github-sync.conf`, then
   `supervisorctl reread && supervisorctl update`.
2. `uv run --no-sync github-sync unwire-git` (removes the gateway git config and the
   hooks path -- auto-push stops).
3. Delete `data/system/github_sync.toml`.
4. If the user chose to delete the remote repo:
   `latchkey curl -s -X DELETE https://api.github.com/repos/<owner>/<repo>`
   (covered by the `github-write-all` granted at enable). If the grant has
   since been revoked, do not re-request it just for this -- point them at
   the repo's GitHub settings page to delete it themselves.
5. Commit the removal. Note that this commit is NOT auto-pushed (the hook is
   inert again).

The apps' dev repositories stay on GitHub, each still named in its app's
record, and stop being updated. Leave them unless the user asks for them to
be deleted too; the same `DELETE` removes one. One the user still wants kept
current is brought up to date on request with
`uv run --no-sync github-sync sync-app <name>`.
