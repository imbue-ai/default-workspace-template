`AGENTS.md` no longer says when to use a skill whose description already says it, and a new test keeps those descriptions within what each harness shows its agent.

- `AGENTS.md` drops from 22,994 to 19,839 bytes: the lines that only named a skill and its trigger are cut, and the triggers no description carried moved into the descriptions. That leaves a workspace about 4,000 bytes of its own additions before Antigravity stops reading the file.

- `system/test_skill_listing_size.py` (new) fails when the names and descriptions under `.agents/skills/` outgrow the listing a harness gives its agent: 30,000 characters on Claude Code and 10,000 tokens on Codex, less the room the harness's own and plugin skills take. Past that budget the harness cuts descriptions without saying so, and the agent stops finding those skills.
