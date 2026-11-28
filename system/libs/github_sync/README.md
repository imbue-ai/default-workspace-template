# github_sync

Opt-in GitHub sync for a workspace. Nothing in this library is active by
default: the `github-sync` skill enables it for a workspace whose user asks
for it, and the skill (not this library) creates the dedicated **private**
GitHub repo and points `origin` at it.

Once enabled, three pieces work together:

1. **The service** (`github-sync run`, supervised as
   `[program:github-sync]`): a wiring + visibility watchdog. Every 60 seconds
   it re-applies the git wiring (self-healing a gateway URL whose
   reverse-tunneled port changed across restarts) and periodically re-verifies
   the sync repo is still **private**, mirroring the answer to a status file
   the post-commit hook consults. The same tick keeps each user app's own
   private repository current (see "App dev repositories" below).
2. **The git wiring** (`uv run --no-sync github-sync wire-git`): global git config that
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
- Pushes go through the latchkey gateway; there is no fallback gateway, so
  while it is unreachable pushes simply fail. A failed push surfaces in the
  hook log (`/tmp/post-commit-push.log`) and is retried on the next commit.
- **Private-only enforcement**: the service re-checks the repo's visibility
  through latchkey every 15 minutes and mirrors the answer to
  `/tmp/github-sync-status.json`; the post-commit hook holds its pushes
  until visibility is first confirmed private and whenever the repo is
  confirmed public. A re-check that fails outright (e.g. the gateway is
  offline -- in which case pushes would fail too) keeps the last confirmed
  answer and is retried every tick.

## App dev repositories

A user app (one with a record under `system/config/apps/`) lives in the
workspace repository at `system/apps/<package>` and can have a private
repository of its own, `<name>-dev`, that holds the app's directory at its
root with the app's own commit history. `app-install link <name>` creates it
and writes its URL to the record's `dev_repo_url`; this service fills it and
keeps it current. It never creates a repository and never commits to the
workspace repository.

On every tick, once the wiring is applied, the service runs the **app pass**
against the workspace's primary checkout. For each app whose record names a
dev repository:

1. It compares the tree of `HEAD:system/apps/<package>` with the tree it last
   pushed. Equal means there is nothing to do.
2. Otherwise it runs `git subtree split --prefix=system/apps/<package>`, which
   turns the workspace commits that touched the app into commits of the app's
   own history, and pushes the result to the dev repository's `main` through
   the gateway. The push is a fast-forward or it is refused; it is never
   forced.
3. It records what it pushed in `data/.state/app_sync/<name>.json`.

The pass writes nothing to the workspace repository, so it cannot disturb an
update that is being applied. The split is made in a scratch clone under
`/var/tmp` that shares the workspace repository's objects
(`git clone --shared --no-checkout`), with hooks off, and the commits it
makes stay in that clone. git copies the branches of a shallow workspace
into the clone instead of sharing them, so the clone of one is then pointed
at the workspace's objects for the rest: an installed app's split builds on
its publisher's commits, which no branch reaches. The push runs in the workspace repository, with
its configuration, and is lent the clone's objects for that one command
(`GIT_ALTERNATE_OBJECT_DIRECTORIES`). The clone is removed when the push is
over, whether or not it went through; one left behind by a service that was
stopped in the middle of a split is removed by the next split.

### Where a split starts

`git subtree split` reads one workspace commit at a time, a few commands
each, about 12 ms a commit where it was measured: three minutes for the
history a workspace starts with. So a split is not given the whole history.
The scratch clone is cut at the app's **bound**, the commit before which
nothing can add to the app's own history:

- for an app this workspace built, the commit its newest join marker was
  made on. The marker says which commit of the app's history that commit
  stands for;
- for an installed app, the merge that installed it
  (`git subtree add --squash`). No commit before it held the app, and that
  merge and the merge of each later update reach the publisher's history
  through their squash commits.

In the clone, git reads the bound as having no earlier workspace commit
before it, and reads every merge made since the bound as following only its
parents that follow the bound (one replace ref each; no object of the
history changes). `git subtree`, which reads history through ordinary git
commands, therefore reads the commits made since the bound and nothing else.

A merge is cut when it brings in commits that do not follow the bound: an
update of the workspace merges the template's history, and a worker's branch
can have left before the bound. Without the cut `git subtree` goes back
through all of those commits' ancestors, one shell call deep per commit, and
the shell stops it ("Maximum function recursion depth") long before the root
of a workspace's history. With it:

- An update's merge, whose older parent never held the app, splits exactly
  as it would if `git subtree` read the whole history: the same commits.
- A change to the app made on a branch that left before the bound is in the
  split, as part of the merge that brought it in rather than as a commit of
  its own. Nothing in the app's directory is lost.

The cut depends on the history alone. The same history gives the same split,
a longer history gives those commits and new ones after them, and the split
starts from the commit the marker names, so a dev repository's history is
continued and never rewritten. `app-install link --refresh` makes its split
through the same cut (`cut_history_at_bound`), so the marker it leaves joins
exactly what the pass has been pushing.

A split still reads every commit made since the bound, so its time grows
with them. For an app this workspace built, `app-install link <name>
--refresh` makes a new marker at the current commit, which the update pass
runs; an installed app's bound stays at its install. A split is given ten
minutes, and one that runs out is reported as `failed` with how many commits
it had to read.

A history that holds no bound is read whole: that of an installed app that
arrived as files in a template or a migrated workspace, with no squash
commit.

### What is never pushed

A split is pushed only when its history is the app's own. When an app's
directory was removed and made again by ordinary commits, `git subtree
split` keeps the commit that removed it as it is, with everything the
workspace held before it, so the split would hand the dev repository the
whole workspace. The pass checks that the split's history does not hold the
workspace's first commit (`require_split_holds_only_the_app`), pushes
nothing when it does, and reports the app as `failed` with the reason. An
app that was uninstalled and installed again is not such a case: its bound
is the newest install, and the removal lies before it.

Which apps the pass pushes:

- **An app this workspace built** is pushed once it is **linked**: the
  history of the checked-out commit holds a join marker for the app's
  directory, a commit whose message carries the trailer
  `git-subtree-dir: system/apps/<package>`. `app-install link` leaves one
  (`git subtree split --rejoin`), and it is the bound every later split
  starts from. Until there is one, the app's first split would read the
  whole workspace history, which the pass never does.
  `git rev-list --max-count=1 --grep='^git-subtree-dir: system/apps/<package>/*$' HEAD`
  is the test, the same search `git subtree` makes, and `app-install link`
  and the pass both ask it through `github_sync.app_history.is_app_linked`.
- **An installed app** is pushed once it has a dev repository, which
  `app-install link` gives it only when it is **modified**: its committed
  directory differs from the tree it was installed from in a path outside
  the regenerable set (`uv.lock`). The squash commits an install and an
  update leave carry the same trailer, so the search above finds them and
  the split builds the adopter's history on top of the publisher's commits.
  `git subtree` refuses to make a `--rejoin` marker for an app that came in
  as a squash, so its bound is its install.

Everything else is skipped and named in the status with a reason:
`no_dev_repository` (the record has no `dev_repo_url`; an app this workspace
built, or a modified installed one), `not_linked` (a dev repository but no
join marker), and `unmodified` (an installed app nobody changed, which has no
dev repository by design).

**Private-only enforcement** covers every dev repository, each on its own:
its visibility is checked through latchkey on the same 15-minute schedule as
the sync repo's, its pushes are held until it is first confirmed private
(`visibility_unknown`) and halted while it is public (`not_private`), and
neither state touches the workspace's own sync or any other app. A push the
dev repository refuses because it holds history this workspace does not
continue (two workspaces that own the same app) is reported as
`push_refused` and retried, never forced. Any other failure (`failed`) is
reported for that app alone.

The state file only saves work:

```json
{
  "dev_repo_url": "https://github.com/alice/notes-dev",
  "pushed_commit": "<the commit of the app's own history that was pushed>",
  "pushed_tree": "<that commit's tree, the app's directory>"
}
```

When it is missing or unreadable (a restored workspace), the pass splits and
pushes again; a split reproduces the same commits, so the push changes nothing
the dev repository already has.

`uv run --no-sync github-sync status` shows the pass under `service`:
`apps` is one object per user app (`name`, `state`, `dev_repo_url`,
`visibility`, `pushed_commit`, `detail`), and `app_pass_error` says why the
pass did not run at all (the service was not started in the primary
checkout), else `null`.

### One app without workspace sync

A single app can have a dev repository in a workspace where GitHub sync is not
enabled. `uv run --no-sync app-install link <name> --create-dev-repo` creates
the repository, and `uv run --no-sync github-sync sync-app <name>`, run from
the workspace root, does for that one app what the service does each tick:
checks the repository is private, splits, and pushes through the gateway. It
prints one JSON line whose `result` is the app's state, with the same fields
as an entry of `apps`, and exits nonzero unless the result is `pushed` or
`up_to_date`. Nothing keeps that repository current afterwards: run it again
after later changes, or enable sync.

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
.venv/bin/github-sync run                      # the watchdog loop (used by supervisord)
uv run --no-sync github-sync wire-git          # install the gateway git wiring
uv run --no-sync github-sync unwire-git        # remove the wiring (disable path)
uv run --no-sync github-sync check-visibility  # print visibility; nonzero unless private
uv run --no-sync github-sync status            # config + latest service status as JSON
uv run --no-sync github-sync sync-app <name>   # split and push one app to its dev repository, once
```

## Library

`app-install` and the publish flows import a few things from here rather
than keep copies:

- `github_sync.repositories`: `create_repository(name, description,
  is_private)` returns `<owner>/<repo>` (`RepositoryNameTakenError` when the
  account has one of that name, `GithubRequestError` for any other refusal
  and when GitHub's answer does not confirm the visibility asked for),
  and `lock_down_repository(repository)` closes issues, wiki, projects,
  discussions and outside pull requests, returning the problem when that did
  not take. `make_repository_public(repository)` makes a repository public,
  raising unless GitHub's answer says it is. `set_repository_topics(repository, names)` sets the topics a
  published template or app is found by, and `hint_for_push_failure(output)`
  says what a push git refused most likely means, when it is a refusal it
  knows.
- `github_sync.app_history`: `is_app_linked`, `is_app_modified`, `app_tree`,
  `is_primary_checkout`, and `require_git_subtree`, which raises
  `GitSubtreeMissingError` naming the command when this git has no
  `git subtree`. `open_app_split(repo_root, record)` is a context that holds
  the app's split (`commit`, `tree`, and the scratch `clone` it was made in)
  until it closes, and `cut_history_at_bound(scratch_clone, record, commit)`
  cuts a clone a caller made itself, as `app-install link --refresh` does
  before it splits with `--rejoin`.
- `github_sync.wiring`: `gateway_git_config()` and
  `git_environment_through_gateway(environment)`, for one git command that
  goes through the gateway without the container being wired.
- `github_sync.config.is_sync_configured(repo_root)`.

The record-level rules both sides read (the regenerable set, the join-marker
pattern, an app's directory) are in `app_manifest.records`, which this
library depends on; `app-install` depends on this library, never the other
way round.
