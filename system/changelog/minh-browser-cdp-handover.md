# Pin @playwright/cli; vendor browser extensions; split the browser-automation guidance

**`@playwright/cli` is now installed in the image**, pinned as `ARG PLAYWRIGHT_CLI_VERSION`
in `system/Dockerfile` and installed by `setup_system.sh`, matching how Claude Code, Codex,
Pi and latchkey are pinned. It is the agent's browser-driving interface now that the browser
service no longer ships driving verbs. (Note it is `@playwright/cli`; the bare
`playwright-cli` package on npm is a deprecated stub.)

**Browser extensions moved into the Fortress env.d unit.** `browser-use` used to download
three extensions from the Chrome Web Store on a user's FIRST browser launch -- into the
browser holding their real logins, chosen by the dependency, with a network call in the
launch path. uBlock Origin Lite and "I still don't care about cookies" are now fetched once
at converge and chosen here; "Force Background Tab" is dropped, since opening links in
background tabs fought the pane's active-tab follow. Deliberately not version-pinned: the CRX
endpoint only serves the current build for an id, so a pin could only ever be a post-hoc
mismatch log. What this fixes is the timing and the ownership, not the version.

**`CLAUDE.md` / `AGENTS.md` now split browser automation by situation**, because picking the
wrong tool was the easy mistake: the fleet for anything the user should see or that needs
their real logins or their help, Playwright's Python API for headless scripting with no human
in the loop. Both run on Fortress, and the Fortress install/gVisor notes are unchanged.

**Root dependencies:** the `openai>=2.20.0` override in `pyproject.toml` is removed. It
existed only to reconcile `browser-use`'s exact `openai==2.16.0` pin with litellm, and
`browser-use` is gone -- along with 228 packages from the lockfile.
