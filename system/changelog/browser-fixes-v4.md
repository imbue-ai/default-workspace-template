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

----

KasmVNC is installed in the workspace.

`system/scripts/env.d/1010-kasmvnc.sh` installs the pinned Debian trixie build on
first boot via the env-converge one-shot, matching the Fortress unit's shape:
sha256-verified, idempotent with a fast satisfied-check, no marker files. It
installs with `apt-get install ./file.deb` rather than `dpkg -i` so the
dependency tail (`xkbcomp`, `xkb-data`, `xauth`, `libxfont2`) resolves -- the
server exits immediately at startup without `xkbcomp`.

CI deliberately does not install it. The real-Chromium integration tests are
already skipped on GitHub Actions for an unrelated reason, so an X server there
would guard nothing.

----

The latency readout is a standalone script, not a fleet-CLI verb.

`python3 system/scripts/browser_latency.py [name] [--watch]` -- stdlib-only, no
venv, no `MNGR_AGENT_ID`. That requirement belongs to the fleet CLI's ownership
verbs; a read-only diagnostic has no owner, and a human at a bare root shell is
exactly who runs it. With no name it reports the whole fleet.
