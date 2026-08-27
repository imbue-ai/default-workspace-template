# Extract the PTY sign-in machinery out of claude/auth.py

`harnesses/pty_auth.py` now owns the parts of a PTY-driven sign-in that are not
claude-specific: spawning a CLI on a pseudo-terminal at a pinned geometry, terminating and
closing it without letting teardown errors escape, replaying the raw stream through a
terminal emulator to recover a value the renderer width-wrapped, reading a value out of an
OSC 8 hyperlink target, and draining the stream until a caller's predicate is satisfied or
the output goes quiet.

`claude/auth.py` keeps everything that is actually about claude -- its regexes, the
settings-env credential writes, `.claude.json` approval recording, the agent-restart
cluster, and the two compositions (`_extract_oauth_url`, `_extract_setup_token`) that bind
claude's patterns to the generic extractors. It is 169 lines shorter and imports the rest.

Three details worth knowing, because the next harness will meet them:

- The frame marker and the PTY geometry are now parameters with claude's value as the
  default. The marker is Ink's synchronized-update sequence; a CLI that emits none is not
  broken, but its replay collapses to a single final-screen snapshot and loses the
  longest-wins protection against a truncated mid-frame candidate.
- `spawn_pty` accepts `env` and `cwd`. `pexpect.spawn` *replaces* the child environment
  rather than merging into it, so a caller scoping a sign-in to one config dir must pass a
  full environment or the child loses `PATH` and never starts.
- `ClaudeAuthError` now subclasses `PtyAuthError`, so the endpoint handlers that catch a
  single type keep working as more harnesses join.

No behaviour change and no user-visible change: same flows, same regexes, same timeouts,
same expect-list ordering.

# Add the account store and the lane table

`accounts.py` owns the account store: one folder per signed-in provider account, with a
random name that carries no meaning, and one index file that is the sole source of truth for
which lane it belongs to and what it is called. The index write is the commit point of a
sign-in, so an interrupted flow leaves a folder with no row (swept at boot) rather than a row
pointing at a half-authenticated folder.

`harnesses/lanes.py` is the table of (AI provider + harness) pairings a user can sign in to,
and how each one signs in. Five lanes -- Anthropic, OpenAI, Google, Opencode Go, and
bring-your-own-key -- over four harnesses, since both Opencode Go and raw keys run on pi.

Every value in that table was measured against the real CLIs rather than read off
documentation, which was wrong about several. The three shapes that fell out:

- **URL out, code back** (Anthropic, Google): scrape the sign-in URL, the user approves in a
  browser and pastes a code.
- **Code out, nothing back** (OpenAI): the URL is fixed, the CLI prints a one-time code the
  user types into the browser, and the CLI polls and exits on its own -- so process exit is
  the success signal, the inverse of every other lane.
- **Paste** (Opencode Go, API key): a file write. No terminal at all.

Success is never scraped off the screen. agy and codex print no success line, so the
harness's own signed-in probe is what decides. Failure IS a pattern, so a rejected code
fails in seconds instead of waiting out a timeout.

# Bind an agent to an account through `mngr create`

`harnesses/binding.py` maps a harness to the one environment variable that scopes it to an
account (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HOME` for agy, `PI_CODING_AGENT_DIR`), the
credential path provisioning writes, and the `mngr create` arguments that repoint it.

The binding happens inside `mngr create` rather than after it, because `mngr create` starts
the agent, waits for readiness, and delivers the first message before returning -- so a
repoint afterwards would land after the first turn had already run on the shared credential.
Two existing flags land at the right moments: `--env` writes the agent env file before
provisioning, and `--extra-provision-command` runs after provisioning but before start.

Two per-account files exist because provisioning only writes them for a per-agent config dir,
and an account folder is ours:

- **claude's `.claude.json`**, via mngr's own `auto_dismiss_claude_dialogs`. Setting
  `CLAUDE_CONFIG_DIR` moves this file INSIDE the dir, so a fresh account folder has no
  onboarding state and boots into the theme/trust dialogs -- which reads downstream as a
  readiness timeout and gets the agent destroyed. Its `keybindings.json` goes with it, or the
  agent silently loses the meta+q interrupt and falls back to a kill.
- **codex's `config.toml`**, pinning `cli_auth_credentials_store = "file"`. Codex keys its
  secret by a hash of the canonical `CODEX_HOME` when the store is a keyring, so without the
  pin a sign-in can write nothing to `auth.json`, leave the bind symlink dangling, and still
  have `codex login status` report success against that same dir.

# Run a sign-in against an account folder

`harnesses/auth_flows.py` drives one sign-in end to end: mint a folder, seed it, run the
lane's method into it, and commit an index row only if the harness agrees it worked. Every
failure path removes the folder, so a half-authenticated one is never offered as usable.

`harnesses/signed_in.py` is the probe that decides. It answers three ways, not two: collapsing
"could not run the probe" into "signed out" would delete a folder the user had just completed
a browser round trip into, since the probes shell out to CLIs that fetch over the network.

Flows are single-flight -- the PTY machinery holds one session, and a user signing in is
doing one thing. Starting a second flow abandons the first.

Two behaviours worth calling out because they are not obvious from the code:

- Nothing advances on its own. The PTY is read when a client polls, so a browser tab closed
  mid-flow would leave a CLI waiting indefinitely (codex's device flow polls for fifteen
  minutes). Every flow arms a wall-clock timer that terminates the process and removes the
  folder.
- Success is never read off the screen; the harness's own probe decides, because two of the
  three terminal lanes print no success line at all. Failure IS read off the screen, so a
  rejected code fails in seconds instead of waiting out the deadline.

codex's API-key alternate is not offered yet: it feeds the key on stdin, and every command
runner in production system_interface pins stdin to DEVNULL. Its ChatGPT device flow is
unaffected.

# Expose the chooser over HTTP

`accounts_endpoints.py` adds `GET /api/lanes` (the chooser's rows and the ways into each),
`GET /api/accounts` (signed-in accounts with the label the picker shows), and the flow
routes: `POST /api/accounts` to start a sign-in, then GET/POST/DELETE on
`/api/accounts/flow/<id>` to poll it, submit a code or key, and abort. `DELETE
/api/accounts/<id>` removes one.

Every method carries its `shape`, so the modal picks one of three screens without needing to
know anything about harnesses. The account label is composed server-side because turning a
harness into "(Claude Code)" needs the lane table, and the client would otherwise keep a
second copy of it.

The flow service is constructed once in `create_application` and read back through the app
state, because it holds the live sign-in PTY between the call that starts a flow and the
polls that advance it.

# The provider chooser

`ProviderChooserModal` lists the providers you can sign in to, one row each; picking one
opens its sign-in screen with the recommended way in on top and any alternates under a
disclosure. It renders one of three shapes -- link-then-paste-a-code, show-a-code-and-wait,
or paste a key -- and the server says which per method, so the client never has to know what
a harness is. That indirection earns its keep on OpenAI, which inverts the usual flow.

It reuses the existing `.claude-login-*` styles rather than growing a parallel vocabulary for
an identical modal; the only additions are a back chevron and a style for the one-time code.

The composer's "Agent auth" button is now "Providers" and opens the chooser. That is a
temporary home -- the chooser's real entry points are the new-tab screen's picker and the
model bar. The transcript auth-error paths still open the old modal for now.

# Make committing an account idempotent so re-authenticating works

Re-authenticating writes an existing account folder a second time. `commit_account`
raised on that, so an expired account could never be revived in place. It now returns
the existing row and only moves the most-recently-used pointer: the seq stays put, so
every agent already bound to that account by label keeps resolving.

# Run a new chat on a signed-in account

A chat created while an account exists for its harness now runs on that account's
credential instead of the workspace's shared login. The binding rides `mngr create`
itself -- `--env` for claude, an `--extra-provision-command` symlink for the rest --
because create provisions, starts, waits for readiness and delivers the first message
before returning, so anything done afterwards lands too late. The chat carries an
`account=<id>` label so the UI can show which provider it is on.

With no accounts the behaviour is unchanged, which is what lets this land before the
new-tab picker exists to name one.

# Show and remove signed-in accounts from the chooser

The chooser lists what is already signed in, with a two-click remove: deleting an account
strands every chat bound to it, so the second click is the confirmation.
