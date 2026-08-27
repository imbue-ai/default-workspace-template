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

# Nothing signed in means the New chat tile opens the chooser

"No accounts" means no credential to launch on, so starting a chat there would produce one
that cannot take a turn. The picker says "No
provider configured" and the tile opens the chooser instead.

`~/.claude` is left alone. An account is only ever reached through the env var a bound
agent carries, so no account is special and nothing outside an agent picks one up: running
`claude` from a workspace terminal is simply not authenticated unless someone signed that
shell in themselves. Special-casing the first account to become the default would have made
one account permanently different from the rest for no reason the model asks for.

The submit-credentials response keeps `auth_mode` and gains `account_id`. mngr's
`test_litellm_via_workspace` asserts on the first and now creates its chat on the second,
because the boot chat it used to reuse binds at create time and never sees a credential
that arrives later. That mirror is imbue-ai/mngr-internal#688.

# Delete the signed-out preflight

Creating a chat no longer probes a harness's CLI first. An account is committed only after
that harness's own probe agreed it was signed in, and a chat runs on the account it binds
to, so "is this harness authenticated" is answered by the account existing. The old gate
probed the SHARED login -- a different credential from the account -- so it would have
refused creates that were about to work fine.

`harnesses/auth_check.py` goes with it. Its probe table duplicated `signed_in.py`'s, which
is the one that decides whether an account is real.

# Spawn agy's sign-in wide enough that the URL cannot wrap

agy prints a ~700-character OAuth URL as plain text. At 80 columns the pinned 1.1.16 wraps
it and emits no OSC 8 hyperlink to recover it from, so the scrape returned the first row --
a URL that still parses, still has a client_id, and is missing `response_type`. Google
answers that with a 401 rather than anything that reads as truncation.

The terminal is now spawned wider than the value (`pty_columns` on the method), which is far
more reliable than de-wrapping rows the CLI may have painted over. `min_length` on the scrape
is the backstop: a short extraction is a fragment, so keep draining rather than hand it over.

Verified against 1.1.16 specifically: 78 characters and no `response_type` before, the full
704 after.

# Spawn a sign-in terminal wide enough that a long value cannot wrap

agy prints a ~700-character OAuth URL. At 80 columns the scrape returned only its first row
-- a URL that still parses and still carries a client_id, but has no `response_type`, so
Google answers 401 rather than anything that reads as truncation.

`pty_columns` on the method spawns the terminal wider than the value, so there is nothing to
de-wrap. Measured across three agy builds: at 80 columns 1.1.16 yields 78 characters and
1.1.22 fails extraction outright; wide, all of them yield the full 704. `min_length` on the
scrape is the backstop -- a short extraction is a fragment, so keep draining rather than
hand it over.

# Restore the sign-in screens to the shape the old modal had

The chooser was rebuilt against the mockup's grammar rather than reduced to the minimum that
worked. Back to being what it was: brand marks in a reserved icon column so every label
lines up, a fixed-height scroll region that remembers its offset across a drill-in and back,
the numbered-step layout with the finished step desaturating, and the "Didn't open? Copy the
link" fallback -- which reveals the raw URL when the clipboard write is rejected, so a
browser handoff that does not fire never dead-ends anyone.

OpenAI's flow inverts the direction (it shows a code you carry to the browser rather than
taking one back), so its second step shows the code with a Copy button instead of a field.
That is the only place the two browser shapes differ; everything around them is shared.

# Port the chooser's UI from the mockup verbatim

The chooser had been re-derived against the older `.claude-login-*` CSS rather than copied
from `prototypes/minds-harness`, which is the design source. This app runs the same Tailwind
v4 and its `@theme` block already carries the mockup's exact hexes, so the mockup's class
strings port across unchanged -- meaning re-deriving them was strictly worse than copying.

`providerSignInStyles.ts` now holds those strings as literals, annotated with the mockup file
each came from, so a later diff against the mockup still means something. The panel, header,
scroll region, chooser rows, option rows, section labels, numbered steps, inputs and buttons
are all the mockup's.

We diverge only where our scope does, and only in the part that differs: no `Runs on`
dropdown or tertiary line (provider -> harness is fixed in V1), the inverted OpenAI step 2
(a code plus Copy, which the mockup has no screen for), and the signed-in / re-auth / delete
rows (the mockup shows one connection). Each still sits inside the mockup's frame.

# Port the sign-in flow from the mockup rather than approximating it

The chooser is now a port of `prototypes/minds-harness` -- `IntroChooserModal`'s row list as
the entry screen, `ProviderSignInModal`'s body behind it -- with the mockup's modes, title
rules, layout rules and class strings. What was missing before, because it had been rebuilt
from the nearest existing CSS rather than copied:

- the spinner screen while a CLI is being spawned and scraped, and again while a submitted
  credential is checked. Both take seconds, and an empty panel reads as a click that missed;
- the "All set" screen with its check, the provider's own mark under it, and a Done footer,
  so every provider gets the confirmation only claude used to have;
- an error screen with the warning glyph and a Try again, instead of a banner over a
  half-drawn form that is no longer actionable;
- the entry screen being a fixed-height scroll region while deep forms flex, so the panel
  holds its size when you drill in;
- the two-step key screen (pick a provider, then paste) with "Saved as ‹VAR› for this mind",
  where before there was one undifferentiated field;
- the header's title naming what you are signing in to, and back stepping one layer.

62 of the 66 `.claude-login-*` rules are deleted with it: everything the modal used to need
is now a mockup class. What is left is the overlay and the spinner's keyframes, which are
genuinely shared. `claudeLogoIcon` goes too -- `providerMarks.ts` owns that artwork now, and
unlike the old helper it takes a size.

# Describe the sign-in terminal as its own, not as the parent's

Every agy sign-in failed in a real workspace -- sixteen seconds, then "Could not read the
sign-in details from the terminal" -- while the identical code succeeded in one second from
a shell in the same container. The difference was one inherited environment variable:
`TERM_PROGRAM=tmux`, from the tmux session supervisord runs the server under.

Node CLIs answer "may I use this feature" from the emulator's NAME, through libraries like
`supports-hyperlinks`. Told it was inside tmux, agy stopped emitting the OSC 8 hyperlink its
OAuth URL is recovered from, and reported nothing wrong -- the flow simply ran to its
deadline. `spawn_pty` now drops the variables that name the parent's terminal
(`TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `TMUX`, `TMUX_PANE`) and pins `TERM`, because the
PTY it creates is not that terminal. Bisected in the failing workspace, one variable at a
time; stripping them takes the flow from failing every time to 704 characters in 1.0s.

# A signed-in account is a fact, not a place to navigate to

The rows were whole buttons, which made them look like they led somewhere. They are plain
rows now, with two named actions beside them: "Sign in again" and remove. Re-auth stays
because an expired credential is otherwise a dead end -- the only other way out is deleting
the account, which orphans every chat bound to it instead of reviving them.

# An account is a row AND a folder; boot makes the two agree

The boot pass only removed folders with no index row. The opposite case turns out to be the
dangerous one, and it happened in a real workspace: a row whose folder was gone. That
account still appeared signed in, the picker still offered it, and a chat still bound to it
-- pointing codex at a `CODEX_HOME` that did not exist, so every model call failed and the
user saw an empty model bar rather than anything naming the real problem.

`reconcile` now settles both directions at boot, and says what it did: unreachable folders
are removed, and a row without a folder is dropped (clearing the most-recently-used pointer
if it named one) so the provider is simply offered for sign-in again. `resolve_account`
refuses such a row outright rather than binding an agent to a directory that is not there.

# Retry a harness's live backend instead of waiting for the next event

A new codex chat rendered blank, with no model bar, and then filled in all at once some
seconds later. The wait was never on codex: its app-server daemon takes a few seconds to
start listening, but the one connect attempt was made when the agent finished being created
-- before that daemon exists -- and the retry was purely event-driven, so nothing tried
again until an unrelated observe event happened along.

The liveness sweep that already runs every three seconds now also asks each tracked agent's
session to bring its backend up. `ensure_live` is idempotent and a no-op for the file
harnesses, so this only ever does work for a session genuinely missing its backend, and the
bar now appears when the daemon is actually ready rather than when the next event lands.

# The composer appears with the transcript, not a moment after it

A new chat rendered with an empty transcript and no composer for a beat, then everything
appeared at once. Two branches were asking the same question -- "is this agent still being
created?" -- and answering it differently. The build log tested `isProtoAgent && not yet
registered`; the footer tested `isProtoAgent` alone. In the window where an agent has been
registered but its proto entry has not cleared yet, the build log had already stood down
while the footer was still suppressed, so the chat rendered with nothing to type into.

`isStillBeingCreated` is now the one definition, and every branch asks it -- the build log,
the footer, and whether the panel accepts file drops. The proto list is rebuilt from
broadcasts and can name an agent that has since registered, so "has a proto entry" was never
the same question as "is still being created"; conflating them is what produced the gap.

# A submitted code goes straight to a waiting screen, and agy can finish at all

Two faults, both on the path after you paste a code -- which is agy and anthropic, the two
`url_then_code` lanes. Codex never hit either because its flow hands a code the other way and
never submits one.

**The flow could not finish.** The probe -- the harness's own "am I signed in" check -- only
ran once the CLI was judged done talking: it had matched a success pattern, or exited, or
died. agy does none of those. It prints no success line and drops straight into its chat TUI,
so a completed sign-in sat at PENDING forever and no code could ever be accepted. A method
that declares no success pattern has the probe as its ONLY possible verdict, so it now runs
while the CLI is still alive -- and only once a code has actually been handed over, since
before the browser round trip the answer is a foregone no and the probe is a network call.

**And the UI fell back to the menu.** The waiting state was `busy`, which covers the request
and not the answer: the server hands the code to the CLI, and the verdict arrives on a later
poll. In between, the modal rendered its menu again -- which reads exactly like being bounced
back to the start. Submitting now holds a "Signing in..." screen until the flow resolves.

The deadline changes with it. The 15-minute budget is for a user away in a browser; once the
code is in, nobody is away, so it drops to two minutes and says which thing timed out.

# Check that the harness accepted a pasted key before calling it signed in

A key-paste lane wrote its file and committed the account in the same breath. Nothing asked
whether the harness could actually use what was written, so a provider id we got wrong or a
schema that drifted produced an account that looked signed in and a chat that silently could
not take a turn.

pi is now probed like every other harness. `pi --list-models`, scoped to the account, names
the provider's models when the credential was read and says "No usable API key is
configured" when it was not -- which is exactly the question worth asking here. It does not
reach the provider, so it distinguishes a well-formed credential from an unusable one, not a
valid key from an invalid one; the honest limit, and still the difference between failing at
the field and failing at the first turn.

A probe that cannot run at all (`UNKNOWN`) still commits: that is the check failing, not
evidence against a key the user just pasted.

`AuthFlowService.create` takes the probe as an argument for the same reason it takes the
spawner. Without that the flow tests were shelling out to whichever CLIs this machine
happened to have, which took them from 0.3s to 13.6s and made them assert on the machine.

Verified against the pinned pi 0.83.0: it reads the `auth.json` we write verbatim, and
`--list-models` under that folder returns only that provider's models -- so "one provider per
folder scopes the model list" holds, and the schema has not drifted.

# The API-key screen spun forever on a mixed-key option list

Picking the API key provider hung on "Signing in..." indefinitely. The request behind it took
six milliseconds; nothing was pending. The provider dropdown built its children as one
unkeyed placeholder option followed by keyed ones, and mithril refuses a list that mixes the
two -- by throwing, mid-render. The throw aborted the redraw, so the screen kept whatever was
last painted, which was the spinner, and no later redraw could replace it.

Worth noting for the next one of these: a render that throws does not look like an error, it
looks like something slow. Nothing appears in a server log, because nothing was asked of the
server. The browser console named it in one line.

# Every provider pi takes a key for, behind the mockup's dropdown

The API-key lane offered eight providers, which was a list someone wrote down rather than a
list pi actually has. Reading pi's own registry -- `pi-ai/dist/providers/<id>.models.js` names
each provider, and the module beside it names the environment variable its key is read from
-- gives 37, of which 28 take a plain API key. That is what the lane offers now, sorted by
name, taken from the registry rather than curated.

The nine left out are left out for a reason, recorded beside the list: amazon-bedrock and the
cloudflare pair want cloud credentials rather than one key, azure needs an endpoint and a
deployment alongside it, google-vertex a project and a location, github-copilot and
openai-codex are OAuth only, and opencode-go already has its own lane.

The provider id is the load-bearing part. `auth.json` is keyed by it, so a display name we
invent is cosmetic while an id we invent is a credential pi will never find -- every id here
is the name of a file in pi's registry.

Twenty-eight rows do not belong in a `select`, so the picker is now the mockup's
`ProviderKeyDropdown`: a trigger showing the chosen provider and its env var, and a menu
pinned under it with its own scroll region. It renders at overlay level rather than inside
the panel, because the panel is `overflow-hidden` and would clip it -- the same reason the
mockup portals it to `<body>`.

# An OpenRouter lane, and the Opencode Go one confirmed

Both are the same thing -- a named provider that runs on pi and signs in by pasting a key --
so OpenRouter is Opencode Go's lane with different strings, and confirming one confirms the
other. Checked against the pinned pi 0.83.0 by writing each `auth.json` and asking pi what it
made of it: both read the key back, and `--list-models` under each folder returns that
provider's catalogue and nothing else. So the mechanism behind the Opencode Go row works, and
"one provider per folder scopes the model list" holds for both.

OpenRouter earns its own row rather than an entry in the generic API-key list because a row
can carry a subtitle saying what the account gets you. The generic row speaks for
twenty-eight providers at once and cannot say anything specific about any of them.

# The key screen no longer flashes a spinner on the way in

Opening a paste lane showed the waiting screen for the length of its request. A terminal lane
spends seconds spawning a CLI and deserves it; a paste lane mints a folder in milliseconds,
so all it produced was a flicker between the click and a form that was always going to be
there. The form renders immediately and the mint lands behind it -- measured in the browser
at 7ms from click to form, with no spinner drawn.

# Sign up for Opencode Go from inside the sign-in screen

Its row said what the plan costs, which is not what the row is for -- the row says what the
account gets you, and the price belongs where you are deciding to buy it.

A paste lane can now carry a `signup_url`. When it does, the screen becomes two numbered steps
-- sign up, then paste -- instead of one field that assumes you already have a key. Step 1
reuses the same link block the browser lanes use, so the copy-link fallback comes with it.

Only Opencode Go sets it. OpenRouter and the generic API-key lane are providers you either
already have an account with or are picking deliberately, so they stay a single field.

# Stop sign-in failures from deleting the account they were signing into

Re-authenticating adopts an existing account folder so that every chat already bound to it
recovers. But every failure path -- abort, a mistyped code, a rejected key, the deadline --
discarded the folder, because a flow could not tell a folder it minted from one it adopted.
Pressing Back during a re-auth deleted the credential and orphaned every bound chat. A flow now
records whether it minted its folder, and only removes its own.

The key paste has the same shape one level down: the harness has to see the file to judge it,
so the write lands before the verdict, and on a live account a rejected key would sit there
breaking every bound agent at its next turn with the row still saying the account is fine. The
previous file is now put back when the answer is no.

An account id arriving in a request body is resolved through the index rather than joined onto
a path. `Path` joins swallow an absolute segment whole and `..` walks out of the accounts root,
and the resolved directory is what the failure paths remove.

# Keep a deleted account's chat history

claude is bound by pointing `CLAUDE_CONFIG_DIR` at the account folder, and claude writes its
session transcripts to `<config dir>/projects/`. So removing an account took every bound chat's
history with it -- the chats did not just stop working, they rendered empty. Discarding an
account now removes its credentials and leaves `projects/` alone, and the boot reconcile leaves
a folder holding only that behind rather than sweeping it as debris. Signing in again is
recoverable; a deleted transcript is not.

# Finish the long-lived token sign-in, and stop the promote gate rubber-stamping

`Get a long-lived token` minted a real 1-year token and wrote it nowhere: the method declared
where the token should be scraped from and written to, and nothing read either field. It now
takes the token off the screen and into the account before asking whether the account works --
`claude setup-token` persists nothing itself, so without that the probe reads an empty folder
and the sign-in fails with a valid token sitting in the pane.

A bare `sk-ant-oat01-` value pasted into the API-key field is filed as a token rather than as an
API key. They are different managed keys and claude reads them from different variables, so the
old behaviour left the account signed out.

A pasted API key is now pre-approved in the account's own `.claude.json`. Interactive claude
challenges any key it has not been told about, and it does so before signalling ready -- so the
agent was destroyed on its readiness timeout rather than starting.

Both promote probes could only answer yes:

- pi's matched text pi prints when asked to take a turn, not when asked to list models. Signed
  out, `pi --list-models` says "No models available" and exits zero, so every key was accepted.
- claude's ran with the server's own environment, and `claude auth status --json` reports
  logged-in on an ambient `ANTHROPIC_API_KEY` alone. On a workspace upgraded from the
  shared-login era, an empty account folder committed as signed in, became the default, and
  launched every later chat with no credential.

# Make the account store safe to share and safe to boot

The index lock was a thread lock, and the server is not the only process that writes there:
`migrate_claude_auth.py` runs standalone and commits an account, and boot's reconcile removes
every folder the index does not name. Two writers interleaving lost an account; one racing the
sweep had its brand-new folder deleted out from under it. The lock is now an flock held across
the whole read-modify-write.

Boot no longer treats an unreadable index as fatal. supervisord restarts this program a million
times, so a truncated write from a hard host kill was an unbounded crash loop with no UI and
therefore no way to delete the offending account.

# One Imbue account, not one per visit

The keys page posts a credential on every visit and each post minted a new account. Re-keying
three times left three rows, two of them holding dead credentials and the newest quietly
becoming the account every new chat launched on. Re-keying now writes into the account it
already owns. That account is labelled distinctly, so it is matched without ever overwriting an
account the user signed into through a browser.

# Selecting the sign-in code no longer closes the sign-in

The chooser dismissed on backdrop *click*, and a click fires wherever the press ended -- so
selecting the device code or the several-hundred-character sign-in URL and releasing past the
dialog's edge read as "close this" and aborted the flow the code was being copied out of. It
uses the shared backdrop helper, which keys off mouse down on the backdrop itself.
