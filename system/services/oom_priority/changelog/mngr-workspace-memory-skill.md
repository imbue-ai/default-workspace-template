`memory_candidates.py` becomes `workspace_memory.py`, one command for every question about memory:

- `free` prints the free memory line (was `--free-memory`).

- `list` prints everything holding memory, largest first, with whether each could be stopped and why not: the chats mid-turn or in use, the main chat, the services agent, the critical apps and workspace services, the browser while a window shows it, and the processes nothing started, which it used to leave out. Shared memory, the files in `/tmp` and a System row close it, so it adds up to the memory in use.

- `list --stoppable` prints only what could be stopped, with the apps a window shows in a second group to be offered last. `list --json` prints the whole report; each section carries what is not a candidate as `others`.

- `show <pid or name>` prints one process, or one chat, app, service or browser with its processes.

- `guard-order` prints the processes in the order earlyoom would stop them, by its own score.

A source it cannot read now leaves the rows it would have decided listed as unknown instead of dropping them. New `oom_priority.processes` holds the process reader and the rule for who each process belongs to, moved from the System Monitor so both use one; `oom_priority.meminfo` gains `read_smaps_memory` and `is_tmpfs`.
