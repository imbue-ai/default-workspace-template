`CLAUDE.md` now documents the browser as an available tool.

Nothing in the system prompt pointed at the browser, so it went unused or got
driven the wrong way. A new "Browser is available as a tool" section records
that a stealth Chromium build is installed in the workspace and which of the two
entry points to reach for:

- The `agentic-browser-fleet` skill for browser-related tasks and any request to
  open a browser. These are collaborative browsers shared by all agents and
  human users, with a mutually-exclusive control handoff and queuing system, so
  the section also covers handing control to the user when help is needed (such
  as anti-bot checks) and releasing it when a task is done.
- Playwright for integration testing and small-scale web scripting -- the same
  Chromium, more lightweight, but single-user: no agent/user collaboration.
