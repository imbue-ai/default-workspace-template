AGENTS.md now says a chat runs the `select-tests` gate only through `system/scripts/run_in_background.py`, never in the foreground, and in the foreground verifies the change as a user would.

The root conftest overrides pytest-playwright's session `playwright` fixture to fail with a pointer to `module_browser`, so a root-suite test that asks for `page`, `context` or `browser` errors on its own instead of leaving an asyncio loop running that breaks every later browser test in its xdist worker. The chat and shell suites, which run their own sessions, are unaffected.
