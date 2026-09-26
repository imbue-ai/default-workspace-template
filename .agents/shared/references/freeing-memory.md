# Freeing memory after a shed

When the memory daemon (earlyoom) sheds something and an agent reports it to
you -- a worker's `question` gate about a shed test command, a worker you find
shed (`lead-proxy.md`, `dead-worker-recovery.md`), or anything else an agent or
the user brings you -- the workspace ran short of memory, and the next heavy
command is likely to be shed too. Memory is the user's to spend, so you find
what could be freed and let them choose; you never stop anything on your own.

1. **List the candidates.**

   ```bash
   python3 system/services/oom_priority/bin/memory_candidates.py
   ```

   It prints the workspace's free memory, the chats and background agents that
   have sat idle (with when each was last active and how much memory it holds),
   and the browsers no window is showing. It only reads; it stops nothing. A
   section it could not read says so: that means "unknown", not "nothing to
   stop". `system/services/oom_priority/README.md` ("Memory candidates") has
   the details.

2. **Offer them to the user.** In plain language, say that the workspace ran
   low on memory and something was paused, how much memory is free, and each
   candidate with what it is, when it was last used, and roughly what stopping
   it would free. Say that a stopped chat comes back by itself the next time
   they send it a message. Ask which, if any, to stop. Never pick for them, and
   never offer the chat they are talking to you in.

3. **Stop only what they approve**, each the way the README's "Memory
   candidates" section says (a chat with `mngr stop`, a background agent with
   the launcher's `stop`, a browser through the browser service), then run the
   candidates command again and tell them what is free now.

4. **Answer whoever reported the shed.** For a worker waiting on a `question`
   gate, reply that it can rerun, or to wait if nothing was freed and memory is
   still falling. If the user declines to stop anything and memory stays low,
   tell them plainly that the checks are paused until memory frees up; do not
   tell the worker to skip them, since memory pressure is never a reason to
   skip a test gate.

Run this once per shed report, not on a schedule: listing is cheap, but every
offer is a question the user has to stop and answer.
