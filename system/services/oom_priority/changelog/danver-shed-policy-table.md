`bands` is laid out as its types, the tables that fill them in, and the functions that read the tables. No band changes, and neither does which process is shed first.

The chat policy's separate tunables become two tables: `CHAT_STAGES`, the stages a chat left alone passes through (for each, the idle time it is reached at, its band, and how much engagement still counts), and `CHAT_ENGAGEMENT`, what each engagement signal is worth. The browser band's three constants become `BROWSER`. Every name other code reads (`CHAT_AGENT_BASE`, `SHARED_BROWSER_FLOOR`, `SERVICE_BANDS`, and the rest) keeps its value.

`CHAT_STAGES` declares a third stage, `aged`, switched off: its idle time is `NEVER`. An idle time in its place is the one value it takes to shed chats left that long ahead of the stale ones. `chat_agent_oom_score_adj` takes the stage table as an optional argument, so a schedule can be tried without editing the module.
