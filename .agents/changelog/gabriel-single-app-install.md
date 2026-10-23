update-self now tests and runs the same app install the services use.

- The shell and chat pre-flights boot `.venv/bin/system-interface` and `.venv/bin/chat-app`, the entry points supervisord runs. Before, they ran the bare names from the agent's PATH, which found a per-app uv tool the services never ran: a stale tool failed the pre-flight and rolled back a good update, and a broken venv could pass it.

- The per-app tool refresh, the snapshots of critical apps' tools, their rebuild on rollback, and the shadowing sweep for app tools are gone. A change to any app's `pyproject.toml` now re-syncs the venv before the pre-flight, so a new console script reaches it. It leaves the mngr tool alone: only `uv.lock`, the root `pyproject.toml` and the plugin manifest reinstall that, so the shell's own `pyproject.toml` no longer does.

- A landed update removes the per-app tools older builds left, from `/root/.local` and `$HOME/.local`, before env-converge re-captures its uv tool record. A rolled-back update leaves them as they were.

- `build-app` no longer installs a new app as a uv tool; its program line execs `.venv/bin/<name>`. `build-app`, `update-app`, `migrate-workspace` and `service-processes.md` now describe one venv install per app.
