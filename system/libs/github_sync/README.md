# github_sync

Opt-in GitHub sync for a workspace. Nothing in this library is active by
default: the `github-sync` skill enables it for a workspace whose user asks
for it, and the skill (not this library) creates the dedicated **private**
GitHub repo and points `origin` at it.

Once enabled, three pieces work together:

1. **The service** (`uv run github-sync run`, supervised as
   `[program:github-sync]`): a wiring + visibility watchdog. Every 60 seconds
   it re-applies the git wiring (self-healing a gateway URL whose
   reverse-tunneled port changed across restarts) and periodically re-verifies
   the sync repo is still **private**, mirroring the answer to a status file
   the post-commit hook consults.
2. **The git wiring** (`uv run github-sync wire-git`): global git config that
   rewrites `https://github.com/...` remotes to the latchkey gateway's git
   proxy and attaches the gateway auth headers, so a plain `git push` works
   for every checkout in the container (main repo and worker worktrees). The
   GitHub credential is injected server-side by the gateway; no token ever
   enters the container. The wiring also points `core.hooksPath` at
   `system/libs/github_sync/git_hooks`, activating the post-commit auto-push hook for
   every checkout.
3. **The post-commit hook** (`system/libs/github_sync/git_hooks/post-commit`, in the
   repo but inert until the hooks path is wired): auto-pushes the active
   branch of any checkout after each commit, so both main-agent and worker
   commits land on the GitHub remote without manual pushes.

What is synced is exactly what is committed to git. Workspace data under
`data/` (memories, tickets, uploads, per-app data) is gitignored and is
NOT shipped to GitHub -- it is covered by the restic `host-backup` service
(see `system/services/host_backup/README.md`), which snapshots the whole home
tree to encrypted object storage.

## Behavior

- Sync is configured iff `data/system/github_sync.toml` exists (it holds
  `repo_url`); the skill writes it. Without it the service idles.
- Pushes go through the latchkey gateway on the user's machine, falling back
  to the per-VPS secondary gateway (remote hosts only) when the user's
  machine is offline. A failed push surfaces in the hook log
  (`/tmp/post-commit-push.log`) and is retried on the next commit.
- **Private-only enforcement**: the service re-checks the repo's visibility
  through latchkey every 15 minutes and mirrors the answer to
  `/tmp/github-sync-status.json`; the post-commit hook holds its pushes
  until visibility is first confirmed private and whenever the repo is
  confirmed public. A re-check that fails outright (e.g. the gateway is
  offline -- in which case pushes would fail too) keeps the last confirmed
  answer and is retried every tick.

## Restoring on a fresh container

If a workspace is recreated from a previously-synced repo, the synced-in
supervisord config already contains the `[program:github-sync]` block, but
the latchkey permissions and the container-local git wiring do not carry
over. The service self-heals: each tick re-applies the wiring, which starts
succeeding as soon as the user re-grants the GitHub permissions (the
github-sync skill walks them through it). Workspace data under `data/` comes
back via a restic backup restore, not via GitHub.

## CLI

```
uv run github-sync run               # the watchdog loop (used by supervisord)
uv run github-sync wire-git          # install the gateway git wiring
uv run github-sync unwire-git        # remove the wiring (disable path)
uv run github-sync check-visibility  # print visibility; nonzero unless private
uv run github-sync status            # config + latest service status as JSON
```
