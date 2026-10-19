# Bug-report collection keeps to a deadline and asks mngr once per kind of thing

Bug reports from busy or large workspaces were arriving with no workspace archive at all ("workspace collection timed out after 300s"): the collector ran `mngr capture` and `mngr transcript` once per agent, about 5s each under gVisor, and the desktop killed the whole exec at its budget with nothing shipped.

- The collector now reads every agent's pane in one `mngr capture` call and every agent's conversation in one `mngr transcript` call, parsing mngr's per-target JSONL records. The members it packs are unchanged: same names, same content, same per-chat grouping, ordering, budgets, consent rules and secret-scan classes.

- It keeps to a deadline, `--budget-seconds` (240s when the desktop app does not pass one). No mngr call starts once too little time is left for the secret scan and packing, a call that runs long is cut off where it would eat into that time, and whatever was gathered before then ships. What the deadline left out is a plain-words line in `collection-notes.txt` (for example "recent chats: 2 of 12 agent conversation(s) were left out because the collection ran out of time").

- mngr is pinned to the release that adds multi-target `capture` and `transcript`, which the collector needs.
