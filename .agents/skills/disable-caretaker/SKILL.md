---
name: disable-caretaker
description: Switch off the weekly Caretaker. Use when the user asks to turn off, pause, or get rid of the Caretaker.
---

# Disable the Caretaker

To switch the Caretaker off, remove its schedule entry -- both the durable
copy and the live one:

    rm -f /home/user/workspace/data/.state/cron.d/minds-caretaker /etc/cron.d/minds-caretaker

That is the whole switch: cron drops the entry within a minute, nothing runs
anymore, and the agent is never woken again. Its notes and permissions file
(under `data/.state/caretaker/`) stay put, so re-enabling later (the
enable-caretaker skill) picks up where it left off.

If the user also wants its state gone, additionally remove
`data/.state/caretaker/` -- but confirm first, since that deletes the Caretaker's
run history and their recorded permissions.
