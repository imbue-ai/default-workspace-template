# Hand the agent CDP, not verbs

Removed `browser-use` entirely. The fleet still owns Chromium -- private display, profile,
cap, ownership lease, streaming -- but agents now drive with `@playwright/cli` over a gated
CDP endpoint instead of our own `state`/`click`/`scroll` verbs.

**Why.** The direct-control layer was built on `browser_use.skill_cli.actions.ActionHandler`,
an undocumented internal upstream deleted in 0.13.3. We were pinned to 0.13.1, the last release
containing it, so the pin could not move -- and the module was already broken: its `scroll` was
`window.scrollBy`, a no-op on any page whose content lives in an inner scroller, reported as
`ok`. That is the bug that started this. It is fixed by deleting the verb: six plain
`playwright-cli mousewheel` commands walk a 200-row virtualized list without any code from us.

**Agent-facing changes.** `agentic-browser-fleet` keeps `new` / `ls` / `close` / `acquire` /
`release` / `handoff` and now prints the `playwright-cli attach --cdp=...` line. The drive verbs
(`state`, `open`, `click`, `input`, `select`, `scroll`, `keys`, `screenshot`, `tab`), the
LLM-driven `task`, and the undocumented `lock`/`hold` are gone, along with `GET /key-status` and
the `key_available` field. **The Anthropic API key is no longer needed for anything in the
browser.**

**Ownership is unchanged in behaviour and re-implemented in mechanism.** `run_action`'s
per-command compare-and-set became a per-frame check in the proxy: the moment a human takes
control the capability token rotates and the agent's next CDP frame is refused. Frames are
*refused*, never the socket closed -- a `@playwright/cli` session whose socket drops is poisoned
permanently (measured: it never rebinds and vanishes from `playwright-cli list`), so closing on
takeover would have bricked the browser on first use.

**The agent's first frame acquires the browser.** `run_action`'s "the first action acquires"
moved into the per-frame gate, so the URL `new` prints drives a resting browser with no
explicit `acquire` -- the sticky lease SKILL.md promises still starts on first use.

**The capability token is issued to one agent.** `GET /browsers/<name>/attach` requires the
`X-Mngr-Agent-Id` header and refuses a browser another agent holds, because the proxy sees a
generic CDP client with no identity. The token stays valid while its own agent holds the
browser and is re-minted the moment anyone else takes it.

**Security.** The proxy authenticates with a capability token in the attach URL, because a
generic CDP client sends no `X-Mngr-Agent-Id` header and agent-vs-agent exclusion would otherwise
be unenforceable. It is a guardrail, not a boundary: the agent has a shell and can read
`DevToolsActivePort` from the profile directory. Accordingly `_unsafe_navigation_reason` (the
SSRF guard on `act_navigate`) is gone -- `Runtime.evaluate` cannot be filtered, so it could not
be preserved, and it never protected the key from an agent that can already `cat` it. Recorded
here deliberately rather than discovered later.

**Extensions move from a dependency's runtime download to our own converge step.** browser-use
fetched three from the Chrome Web Store on a user's FIRST browser launch, into the browser
holding their real logins, chosen by the dependency. uBlock Origin Lite and "I still don't care
about cookies" are now fetched once at converge and chosen here; "Force Background Tab" is
dropped (it fought the pane's active-tab follow). Not version-pinned: the CRX endpoint only
serves the current build for an id, so a pin could only be a post-hoc mismatch log. What this
fixes is the timing and the ownership, not the version.

**Pre-existing bugs fixed along the way.**

* An unexpected `[program:browser]` restart orphaned Chromium; the restore path then cleared the
  singleton locks the *running* browser held and launched a second Chromium onto the same
  profile -- two writers, and an orphan invisible to OOM retagging, so earlyoom would shed the
  agent before the browser. The launcher now reaps an orphan before launching.
* `DevToolsActivePort` was not cleared with the other singleton files, so a relaunch could read
  the previous run's debug port.
* `SKILL.md` documented a 90s idle lease; it is 60s.
* `anthropic_key_status` advertised an `extract` verb that has never existed.
* `lock` / `POST /hold` were reachable and tested but documented nowhere.

**Dependency effect.** 228 packages leave the lockfile, including the entire LLM SDK tree
(`anthropic`, `openai`, `google-genai`, `groq`, `ollama`) that `browser-use` pinned exactly. The
`openai>=2.20.0` override in the root `pyproject.toml`, which existed only to reconcile
browser-use's pin with litellm, is removed.
