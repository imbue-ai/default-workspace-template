update-self now tests and runs the same app install the services use.

- The shell and chat pre-flights boot `.venv/bin/system-interface` and `.venv/bin/chat-app`, the entry points supervisord runs. Before, they ran the bare names from the agent's PATH, which found a per-app uv tool the services never ran: a stale tool failed the pre-flight and rolled back a good update, and a broken venv could pass it.

- The per-app tool refresh, the snapshots of critical apps' tools, their rebuild on rollback, and the shadowing sweep for app tools are gone. Any app's `pyproject.toml` now counts as a backend manifest, so a new console script reaches the venv through a sync before the pre-flight.

- A landed update removes the per-app tools older builds left, from `/root/.local` and `$HOME/.local`, before env-converge re-captures its uv tool record. A rolled-back update leaves them as they were.

- `build-app` no longer installs a new app as a uv tool; its program line execs `.venv/bin/<name>`. `build-app`, `update-app`, `migrate-workspace` and `service-processes.md` now describe one venv install per app.
