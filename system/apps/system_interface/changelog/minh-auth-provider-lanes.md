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

# Replace the harness feature flags with the provider picker

`FEATURE_FLAG_ENABLE_OTHER_HARNESSES` and
`FEATURE_FLAG_ENABLE_INTRODUCTORY_AGENTS_IN_OTHER_HARNESSES` are gone, and with them the
whole meta-tag injection they were the only users of, plus `flip_feature_flags.sh`.

The new-tab screen now offers one **New chat** tile and a **Provider** picker under it.
The picker lists the accounts you have signed in and an "+ Add provider" row; the chat
starts on whichever is selected, and the rail's Chat shortcut reads the same selection so
the two cannot disagree. With nothing signed in the picker names the workspace's own
login, and a chat started there behaves exactly as it did before accounts existed --
"no accounts, no change". Sending an empty picker to the chooser instead is part of the
first-run redesign, which is the same change that removes the shared login it would
otherwise be hiding.

Launching bumps that account's most-recently-used marker, so the picker offers it again
next time. `first` is gone from the frontend: that create template belongs to the
workspace's own first run, not to a tile anyone clicks.

# Do not wait for a value the keystroke pacing already read

Pacing the keystrokes reads the PTY, so on a CLI that answers the moment Enter lands the
scraped value can arrive before we ask for it -- and `expect` cannot match bytes another
read has already consumed. agy hit this every time: its URL was pulled in by the key-gap
drain, and the flow then timed out waiting for it with the answer in hand. The trigger is
now checked against what we already hold before the stream is waited on.

`AuthFlowService.create` takes an optional `spawner`, matching ClaudeAuthService's
`pexpect_spawner`, so the case has a test instead of only a manual run.

# Delete the old claude sign-in modal and everything that opened it

The provider chooser is the only sign-in surface now. Gone with the modal: its models
(`ClaudeAuth.ts`, `AgentAuth.ts`), the terminal-instructions notice that stood in for
harnesses with no in-app flow, and every path that opened one without being asked -- the
page-load status check, the live auth-error hook on the transcript stream, and the same
check on snapshot load. Signing in is something the user asks for.

The composer still refuses `/login` and `/logout`, for a better reason than before: sending
one would run the agent's own auth flow inside its terminal, where we cannot see the result.
The notice now offers the provider chooser, which signs in to a fresh account and leaves the
agent's own credential alone.

# Adopt a pasted credential as an account instead of overwriting the shared login

`/api/claude-auth/submit-credentials` -- the endpoint the Electron chrome POSTs after the
user visits the Imbue keys page -- now mints an account of its own. It used to overwrite
the workspace's shared `settings.json` and restart every claude agent so they would see it;
nothing running is disturbed now, and the account's existence is the signed-in-with-Imbue
flag rather than a separate thing to record.

The endpoint stays where it is because it is a cross-repo contract, as does
`/api/claude-auth/status`, which mngr's deployment test uses as a readiness probe. The six
routes that only ever served the deleted modal are gone.

The claude key field takes an env-file paste as well as a bare key, so a proxied setup --
which only means anything with `ANTHROPIC_BASE_URL` alongside its key -- can be expressed.
Both shapes go through the same strict parse that rejects unmanaged keys and mixed modes.

Abandoned sign-in folders are swept at boot: a minted folder with no index row is
unreachable by definition, so nothing else would ever look at it.

# The account decides the harness, and an expired one can be signed in again

`CreateChatRequest` no longer takes a harness. It was possible to ask for a codex chat and
an agy account in the same call, and nothing would notice until the first turn failed; the
harness now comes from the account's lane, so the two cannot disagree. With no account the
answer is claude, which is the workspace login -- unchanged from before accounts existed.

Clicking a signed-in account in the chooser re-authenticates it in place. Same folder, same
id, so every chat bound to it by label can take a turn again instead of being orphaned by
an expiry.

Tests get their own accounts root (`MINDS_ACCOUNTS_ROOT`, set by the isolation fixture).
Without it a test run wrote into the developer's own `~/.minds` -- a chat create resolves an
account several calls down -- and the leaked account then bound every later create in the
session. That is not hypothetical; it happened while writing this.

# Delete the claude auth service's now-unreachable half

With the modal gone and the paste repointed at the account store, everything in
`ClaudeAuthService` except `get_auth_status` was unreachable: the setup-token and OAuth PTY
flows, the agent-restart cluster, and the shared `settings.json` writer they fed. All of it
is deleted, along with the restart-progress fields that only ever existed to drive the
modal's spinner. `auth.py` goes from 1358 lines to ~750.

The lane table covers those flows now, and it covers them for every harness rather than
just claude.

# The first claude account becomes the workspace's default login

`~/.claude` is repointed at the first claude account signed in. Workspace terminals,
supervisord services and `claude_p.py` -- which eight skills plus build-app and
migrate-workspace call for scripted steps -- all resolve that path with no agent to bind,
so without this they keep running on whatever was there before while every chat uses an
account.

Anything already at `~/.claude` is moved into the account first, then replaced by the link.
A plain `ln -sfn` onto a real directory links INSIDE it, and a forced one drops `projects/`
-- where the transcript watcher and mngr's resume gate both look. Only the first account
adopts it; a later one is an additional account, not a new default for every skill.

# Nothing signed in means the New chat tile opens the chooser

With the default login now being an account, "no accounts" really does mean no credential,
so starting a chat there would produce one that cannot take a turn. The picker says "No
provider configured" and the tile opens the chooser instead.

**Breaking:** an existing workspace's `~/.claude` moves into an account the first time
anyone signs in. Running chats keep the credential they started with; new ones use the
account.
