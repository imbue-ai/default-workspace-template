The `work-on-linear` skill now tells the agent how to write a PR's title and
description when the user asks it to open one. Titles lead with the ticket tag
(`[MIND-123] ...`, dropped entirely when the work has no ticket) and name the
change's subject and outcome in plain English, reserving category prefixes for
strong ones like `Deflake:`. Descriptions cover what was wrong, why, what changed
(and what did not), and how it was checked.
