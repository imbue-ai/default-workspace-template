Added `bin/memory_candidates.py`, which a lead runs after one of its agents reports a shed. It lists what could be stopped to free memory, next to the free memory `/proc/meminfo` reports. It only lists and never stops anything.

- Idle chats and workers: from `mngr list`, a local chat or worker that is `WAITING` and has had no activity for 15 minutes. Each shows its last activity and the summed RSS of its processes (the pid mngr reports plus every pid the agent-pid registry holds for it).

- Browsers no window shows: a running browser the shell's desktops do not name, with the RSS of its Chromium processes.

- A source that cannot be read (`mngr list`, the browser service, the shell) is reported as unknown in its own section. The other sections are still listed.

The README's new "Memory candidates" section covers how to stop what the user approves: a chat with `mngr stop` (it restarts on its next message), a worker with `create_worker.py stop`, a browser with the service's stop route.

The registry gained `live_pids_by_agent_id`, which returns every live pid of each agent; `lookup_pid_by_agent_id` now uses it. `agent_identity` exposes its label names and `is_label_true`, so a caller that already holds an agent's labels can classify it. `app_registry` gained `read_app_url`.
