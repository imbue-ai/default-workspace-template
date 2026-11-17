- An isolated instance booted from a git worktree now runs over copies of the user's live data. Before, `serve_isolated_instance.py --copy` and `preview_app.py` looked for an app's data, its secrets and the registry a shell preview copies inside the worktree, which has none of them, so a worker's instance silently started on an empty store with no secrets.

- `preview_app.py up` and `serve_isolated_instance.py` refuse to register a window from a git worktree, rather than writing it into the worktree's own registry, which the workspace never reads, so the user never saw it. `preview_app.py up --private` boots the instance with no window, for an agent to drive on its loopback port; it takes no `--with` siblings and will not replace a preview that has a window. The worker guide now uses it.

- A preview window's default title is now `<App> (preview)` instead of the app name followed by the worktree's directory name.
