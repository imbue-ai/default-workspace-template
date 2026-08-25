# Browser skills: own with the fleet, drive with playwright-cli

`agentic-browser-fleet` no longer drives browsers -- it owns them. The skill now covers
`new` / `ls` / `close` / `acquire` / `release` / `handoff` and the human-takeover etiquette,
and `new` prints the `playwright-cli attach --cdp=...` line to drive with. The driving half
of the skill (the loop, element indices, the requery rule, the command table) is gone, along
with the `task` verb and its Anthropic API key requirement.

Added `.agents/skills/playwright-cli/` -- Microsoft's own skill from `@playwright/cli`,
vendored verbatim so it stays in git and is pinned alongside `PLAYWRIGHT_CLI_VERSION`, with
a workspace-specific addendum in front of it covering five things the upstream text does not
know about this environment:

* Never run `close` / `attach` / `detach` / `close-all` / `kill-all` / `delete-data` -- the
  fleet owns lifecycle, and a killed session is unrecoverable.
* A dropped session is poisoned permanently: it never rebinds and every command then hangs.
* On ANY failure, do not interpret the message -- `playwright-cli` exits 1 for everything, so
  run `agentic-browser-fleet ls` and branch on what the fleet says.
* `snapshot` prints inline (5-15k tokens); prefer `find`, `eval`, or `--filename=`.
* `mousewheel` scrolls at the last mouse position, default (0,0) -- `hover` the pane you mean
  first, or it silently scrolls the wrong one.

The fleet skill's idle-lease figure is corrected from 90s to 60s, which is what the code has
enforced for some time.
