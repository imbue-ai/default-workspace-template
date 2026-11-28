---
name: free-workspace-memory
description: Use when the workspace is low on memory or slowing down, when the memory guard stopped ("shed") a command, chat or agent, whether yours or one an agent reports to you, or when the user asks what is using memory or what could be stopped. Lists what holds memory, measured the way the guard does, and frees it only with the user's approval.
metadata:
  author: imbue
---

# Freeing workspace memory

The workspace has a fixed amount of memory, and a memory guard (earlyoom) stops the most
expendable processes when it runs low. When it sheds something -- a command of yours, a worker's
test command, a worker itself -- the workspace ran short, and the next heavy command is likely to
be shed too. Memory is the user's to spend: you find what could be freed and let them choose. You
never stop anything on your own.

One command does every measurement here, the way the guard measures. Use its figures and no
others: `ps`, `top`, `free` and `/proc/meminfo` read several times wrong in a workspace.

1. **List what could be stopped.**

   ```bash
   python3 system/services/oom_priority/bin/workspace_memory.py list --stoppable
   ```

   The first line is the free memory the guard decides on, and how much of it is left before the
   guard starts stopping processes (it acts at a share of the total, not at zero). Under it are
   two groups, each largest first:

   - **Can be stopped**: chats and workers nobody has used for a while, apps no window shows,
     and the browser while no window shows it. A chat mid-turn or waiting on a background task is
     never here, and neither is the main chat.
   - **On screen now**: apps a window shows. Stopping one leaves its window reading "Starting"
     until the page is reloaded, which also starts the app again.

   A line starting `unknown:` means a group may be missing rows, not that it is empty.

2. **Offer the first group to the user.** In plain language: the workspace ran low on memory and
   something was paused (when that is what happened), how much is free, and each thing in the
   group with what it is, when it was last used, and how large it is next to the others. Name
   each as the user knows it (the chat's title, the app's name, "the browser"), never by its
   process or program, and give sizes as "about". Say that a stopped chat comes back by itself
   the next time they send it a message. Ask which, if any, to stop. Never pick for them, and
   never offer the chat they are talking to you in.

   Bring up the second group only when the first does not free enough, or the user asks, and say
   what stopping one does to the window they have open.

3. **Stop only what they approve**, each by the NAME its row carries:

   | What | How |
   |---|---|
   | a chat | `mngr stop <NAME>` |
   | a worker | `uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py stop --name <NAME>` (a plain `mngr stop` would look like a crash, and it would be restarted) |
   | an app | `supervisorctl stop <NAME>`, and `supervisorctl start <NAME>` to put it back |
   | the browser | `curl -sf -X POST http://127.0.0.1:8090/browsers/<NAME>/stop`, which keeps its tabs |

   Then read what is free now, and tell them:

   ```bash
   python3 system/services/oom_priority/bin/workspace_memory.py free
   ```

4. **Answer whoever reported the shed.** For a worker waiting on a `question` gate, reply that it
   can rerun, or to wait if nothing was freed and memory is still falling. If the user declines to
   stop anything and memory stays low, tell them plainly that the checks are paused until memory
   frees up; do not tell the worker to skip them, since memory pressure is never a reason to skip a
   test gate.

Run this once per shed report or request, not on a schedule: listing is cheap, but every offer is a
question the user has to stop and answer.

## When the question is only what is using the memory

The same command answers, and nothing needs stopping:

```bash
python3 system/services/oom_priority/bin/workspace_memory.py list                  # everything, largest first
python3 system/services/oom_priority/bin/workspace_memory.py show <pid or name>    # one process, or one chat, app or browser
python3 system/services/oom_priority/bin/workspace_memory.py guard-order           # what the guard would stop next
```

`list` adds up to the memory in use, and says of each row whether it could be stopped and why
not. `show` takes a pid, or the NAME or TITLE a row carries. `guard-order` is of single processes:
the guard stops one process at a time, not a whole chat or app.
