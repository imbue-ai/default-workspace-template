# Linking a new app

A new app is committed as soon as it is registered: its directory, its record
(`system/config/apps/<name>.toml`), its program file, and any skill link. This
file is what follows that commit in a workspace with GitHub sync on.

GitHub sync is on when `data/system/github_sync.toml` exists
(`test -f data/system/github_sync.toml`): it is the file the `github-sync`
skill writes. With sync off, do nothing here. An app is then just its
directory, and it is linked when sync is enabled or at the next workspace
update.

With sync on, run once, from the repo root of the live workspace:

```bash
uv run --no-sync app-install link <name>
```

It gives the app's own history its starting point and creates the app's
private dev repository, `<name>-dev`, which the sync service then keeps
current (the `github-sync` skill describes dev repositories).

- It needs the app to be committed first.
- It is refused in a worker's worktree, since it moves the workspace's own
  branch.
- Its result is one JSON line. `linked` is the expected `result`, and
  `problems` names anything it could not do (a repository of that name already
  on the account, say). A problem does not hold the build back: mention it to
  the user at the end and carry on.
