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

# Smaller fixes to the sign-in screens

- Submitting a code or a key while the flow is settling raised rather than saying what
  happened: the teardown had already released the terminal.
- A key-paste flow armed no deadline, so a closed tab left it pending and its folder on disk
  until the next sign-in or the next boot -- and the service is single-flight, so that flow was
  in the way of the next one.
- A flow whose last check could not run keeps its folder, and the deadline no longer discards
  it anyway. The two mechanisms used to contradict each other.
- Account folders are created 0700. The CLIs write credentials straight into them.
- A poll that 404s is terminal. The server no longer has that flow, so no later tick will say
  anything different, and swallowing it left the screen spinning forever.
- A poll or submit that outlives its flow no longer stamps its state onto the next one.
- Signing in selects the account that was just created, so someone who picked an account
  earlier and then added a provider does not silently get the old one.
- "Try again" works. It re-rendered the identical error screen for the seconds a terminal takes
  to spawn, because the previous attempt's failure was still on screen -- and each extra click
  started another sign-in and killed the previous one.
- A failed provider load offers a retry instead of a permanent "Loading providers...", and a
  failed account removal says why instead of leaving the row silently in place.
- Re-authenticating updates the account's provider name. Re-keying to a different provider left
  every label naming the old one.
- Accounts are numbered by what their label says, not by lane. Two lanes run on pi and could
  both mint an OpenRouter account, giving two rows reading "OpenRouter (Pi)"; meanwhile a lane
  offering many providers numbered its only Groq account "Groq (Pi) 2".

# Delete what the old sign-in left behind

`HarnessSpec.auth_modal` and `auth_instructions` are gone, along with `AuthModalKind` and the
two `/api/harnesses` fields carrying them. They described a per-harness auth surface that no
longer exists -- every harness signs in through the same chooser -- and they were still telling
the client to send users to a terminal.

`claude/auth.py` keeps only its read side: `get_auth_status`, and the vocabulary of a claude
credential (the three managed keys, the env-lines parser, the API-key approval). The
setup-token session record, the OAuth-provider and flow-kind enums, the restart snapshot and
its continue message, and the pexpect spawner all left with the modal. Its docstring described
a system that had been deleted; it now says what is left and where the rest went.

`EofPolicy.RESCAN` never reached a terminal state -- the settle path tests success and failure
only -- so its only effect was to hold a finished claude sign-in pending for its whole deadline.
Both claude methods use `FAILURE`, which is what they were already relying on.

`PasteSink.CODEX_STDIN` and `CreateChatRequest.first` had no producer at all.

One default-account rule instead of three. `resolve_account` answers an explicit id only;
"which account should a new agent use" belongs to `resolve_binding`, which knows which lanes
this build has. The live path now also gets the folder check the explicit path always had, and
it agrees with what the picker shows -- the launcher and a new project's starter chat could
previously land on different providers with nothing saying so.

# Register the mockup's token names, so "port it verbatim" is a rule you can follow

The design source writes `text-tertiary`, `border-subtle`, `bg-fill-hover`. This app's theme
says `--color-text-faint`, `--color-border`, `--color-bg-hover`. Same intent, different names --
so a "verbatim" port was being hand-translated hex by hex, and 62 arbitrary-value classes
(`text-[#202020]`) had accumulated in the file whose own docstring says the class strings were
copied unchanged.

The translation fails silently: Tailwind v4 emits nothing at all for an unknown color utility,
so `hover:bg-fill-hover` against an undefined token simply has no hover state. No build error,
no lint error, no type error, nothing to see in review.

The mockup's names are now registered over this app's existing palette, the 62 literals use
them, and a test asserts every color utility in the ported styles resolves to a defined token.
Values stay this app's: the mockup is two-layer light/dark and this app is light only, so a
token whose point is the dark half is deliberately absent rather than faked.

# Groundwork for the combo card

Two things the model-bar replacement needs that were not there.

`flyout-position.ts`: pure geometry for a submenu that opens BESIDE a panel, top-aligned with
the row that triggered it. Its sibling `dropdown-position.ts` only does the horizontal case (a
popup under its trigger); a flyout needs both axes, a height cap, and a flip to the other side
when there is no room. Six tests cover the placements that are easy to get wrong.

`ModelBar.test.ts`: a render smoke test over every branch of the CURRENT bar -- empty, no
catalog, matched model, the shrug, read-only, effort, fast, and a dynamic harness with no
static options. Written against today's component on purpose, so the rewrite has a before and
after to compare rather than a blank page. It asserts what the user can see and click, not
internals, so it should survive a faithful rewrite and fail on an unfaithful one.

Writing it found that a read-only slot is deliberately still a `<button>`: a disabled one
suppresses `:hover` and would kill the tooltip explaining why the model cannot be switched.

# Test what nothing was testing

Three gaps let every bug above ship green.

`FakePexpectProcess.isalive()` was hardcoded true, so every "the CLI has exited" arm was
unreachable from tests -- including codex's device flow, whose only success signal IS process
exit. It is scriptable now.

The provider routes had no request-level tests: seven routes composing hand-written dicts
against a hand-written TypeScript interface, with no codegen and no schema, so renaming a key
passed the type checker, the linter and both suites while every row rendered `undefined`. The
new tests pin the key sets and the status codes. They also caught that a bad lane id returned
a double-quoted message, because the error subclasses `KeyError` and `str()` adds its own.

The chooser and the model bar had no test files at all. Both now have render smoke tests, run
under a real DOM: mithril validates the keyed/unkeyed fragment rule during its DOM diff, not
while building vnodes, so walking the tree cannot see the crash that froze this exact component
on a spinner. Verified by reintroducing it -- the test fails with mithril's own message.

And a trap that would have bitten the first person to write one of those route tests: the test
app was wired with the production sign-in probe, which shells out to whatever claude/codex/agy/
pi the machine has, over the network. It defaults to "could not run" now.

An unknown lane id came back from the API wrapped in its own quotes -- `"'no such lane: bogus'"`
-- because the error subclassed `KeyError`, whose `__str__` is `repr(args[0])`. It is a
`LookupError` now, so a message written for a person arrives as one. Found by the first
request-level test written against these routes.

# Open the chooser on a specific account

`openProviderChooser` takes an optional account id. With one, the chooser lands directly on that
account's sign-in rather than on the lane list -- which is what a dead-account notice and a
per-provider card both need, and what the account row's own "Sign in again" button now uses so
there is one path rather than two.

# Every chooser row says what the account gets you

Anthropic and OpenAI had no subtitle, on the reasoning that a familiar provider name says
enough. It does not: someone deciding between these rows wants to know whether the plan they
already pay for is usable here, and OpenAI in particular has a free tier with limited coding
usage that is worth naming. Opencode Go's line carries its price, and OpenRouter's says "any
model", which is what it actually offers.

# Signing in from the new-tab screen opens a chat on what you signed into

Adding a provider from the new-tab screen -- either through the picker's "+ Add provider" or by
clicking New chat with nothing signed in -- now opens a chat on the account once the sign-in
finishes. You asked for a chat and had to authenticate on the way; being returned to the
launcher with a provider and no chat is not what was asked for.

Adding a provider from inside a chat still just adds it. You were working, and a new chat
appearing on top of that is the wrong answer.

The workspace's very first chat carries the `first` template, which is what delivers `/welcome`.
Bootstrap used to own that by creating a chat at boot; it cannot now, since a chat needs a
provider account and a fresh workspace has none. The claim lives in the account store, is made
once per workspace, and survives signing out of everything -- being welcomed a second time reads
as the workspace having forgotten you.

# Delete the welcome resender

It existed to re-send `/welcome` to the boot chat when the workspace's initial greeting failed
for lack of credentials. Its caller -- the auth-success chokepoint in the sign-in modal -- was
deleted with the modal, so the module has had no live path since; and there is no boot chat to
address now either. `/welcome` reaches the first chat through the `first` create template, on a
chat that has an account by construction, so there is nothing left to recover from.

# Creating a chat with no provider is refused, not bound to nothing

`resolve_binding` returned None when nothing was signed in, and the caller created the agent
anyway -- one that could not take a turn and said nothing about why. It raises now. There is no
shared login behind that None any more: the settings-env writer is deleted and `~/.claude` is
left alone.

Every surface that starts a chat checks first and opens the chooser instead, including a new
project's starter chat -- which previously turned the failure into a blocking browser alert on
every project creation. Signing in from there finishes what was asked for and starts the chat.

# A re-authentication that fails now says so

Re-auth drove the CLI against a folder that still held the old credential, and three of the
four promote probes are presence checks rather than validity checks -- `claude auth status
--json` reports `loggedIn` for a bogus key, and codex and pi behave the same. So the account
someone was trying to fix answered on behalf of the sign-in they had just abandoned: decline in
the browser, and the modal said "Signed in again. Every chat on this provider can take a turn
once more." Nothing had changed.

The credential is taken away before the CLI is driven, so the probe judges the new sign-in.
Every path that does not end in a fresh one puts it back -- failure, abort, a second sign-in
displacing this one, the deadline -- because the credential the account had is more use than
none, and the user asked to replace it rather than to lose it. The one exception is a check
that could not run at all, where a sign-in may genuinely have landed.

# Codex, pi and Antigravity can say when the credential is the problem

All three reported `is_auth_error: False` unconditionally, each with a comment saying the error
shape was unknown. Driving the pinned CLIs against a deliberately bogus credential answered it:

- codex writes the failure to the transcript after all, as `task_complete.error.message` -- its
  parser's comment said auth errors lived only in `logs_2.sqlite`;
- pi ends the assistant message with `stopReason: "error"` and `errorMessage` carrying the
  provider's raw body;
- Antigravity already surfaced an error step; it just never asked what kind.

One shared vocabulary rather than three copies, matching on what does not change when a
provider rewords itself: the HTTP status, the structured error type, and the handful of phrases
the CLIs use in their own words. It deliberately does not flag a rate limit, a network failure
or a model refusal -- those end a turn too, and offering "sign in again" for them teaches the
user to ignore the notice.

# A dead account says so, and re-authenticating brings its chats back

A turn that fails on an authentication error now renders a "sign in again" action under the
error, which opens the chooser on that chat's own account -- so the fix is one click from where
the problem appeared. Inline rather than a modal: an auth failure used to throw the sign-in
modal over whatever you were doing, and that is what made it hated. This waits to be clicked.

A successful re-auth restarts every agent bound to that account. They do not pick up a swapped
credential on their own -- claude reads its settings env at process start, and nothing
establishes that codex's daemon re-reads its auth file either. One rule for all of them rather
than a per-harness table built on untested assumptions: a restart after a deliberate sign-in is
cheap, and guessing wrong the other way leaves a chat dead with nothing on screen to say why.
A FAILED re-auth restarts nothing -- the account still holds the credential it had.

# Finish the mockup's token layer: type and elevation, not just colour

Registering the mockup's colour names covered a third of the problem. Its markup also uses
`@utility type-*` -- one utility bundling size, weight and line-height -- and `shadow-overlay`
for anything floating above the page. Neither existed here: `grep -c` returned 0 while the
combo card's source uses `type-helper` eleven times and `shadow-overlay` three.

Both fail exactly the way the colours did. Tailwind v4 emits nothing at all for an unknown
utility, so a ported row silently takes the inherited font size and a floating panel renders as
a white box on white with a hairline border. No build error, no lint error, no type error.

Both are copied verbatim, and the style guard now checks utilities and shadows alongside
colours -- verified by renaming one and watching it fail, which the first version of the
assertion did not catch because it matched on a prefix.

# The model bar becomes a combo card

Provider and model in one card under the composer, ported from the mockup. The provider was
never shown before because there was only ever one; now there can be several, and which one a
chat runs on is the first thing about it.

Every provider that is not this chat's is greyed, and clicking one opens a NEW chat on it. A
chat binds to its account when it is created and nothing rebinds it, so there is no state in
which switching in place would work -- offering it would be a control that silently does
nothing.

Three states have no model, not one: the catalog may not have loaded, the live choice may not
have resolved (every harness passes through this before its first model read, and opencode
never leaves it), or the live model may match no catalog option. The provider row renders in
all three, because a provider belongs to the account rather than to the model.

The card and its flyout portal to `<body>`: the chat panel sits inside dockview's clipping
overlay, so anything extending past the panel would be cut at its edge.

Three deliberate divergences from the mockup, each for a reason the prototype could not have:
the flyouts open on CLICK rather than hover, because hovering would fire a `pi --list-models`
subprocess or a codex daemon connect on every pointer sweep across the card -- which also makes
the mockup's ~60 lines of safe-triangle hover-aim arithmetic unnecessary; the effort slider
commits on release rather than per notch, because each notch is a live switch typed into the
agent's pane and the switch queue chains rather than debounces; and a model with one effort
stop gets no slider at all, because pi's non-reasoning models declare exactly one and an
immovable full-green track labelled "Off" says the opposite of the truth.

# Delete "Powered by"

The card names the provider, so a separate credit line under the composer says the same thing
twice -- and says it less usefully, since it named the harness rather than the account.

`dropdown-position.ts` goes with it. It placed the old bar's dropdowns on one axis; the card's
flyouts need both, a height cap and a side to flip to, which is `flyout-position.ts`, and
nothing else wanted the old one.

# Make the pickers actually respond

The card and its flyouts are drawn through `Portal`, which uses `m.render`. That is mithril's
manual API and it deliberately does not wire auto-redraw into event handlers -- only `m.mount`
does. So every handler inside a portalled popover changed state and nothing re-rendered: a row
click opened no flyout, a trash click armed no "Remove?", and the click after that read as
outside and tore the whole stack down. It looked like a dozen separate bugs and was one.
`Portal` now re-adds what `m.mount` would have.

Outside-click asks the DOM (`closest` for a popover marker) instead of element references
captured in `oncreate`. A reference that was stale or arrived late made an inside click read as
outside, and the popover died on mousedown -- before the click it was meant to act on landed.

Tooltips move to `hoverTooltip.ts`, the app's body-level bubble. The chip ported from the mockup
sat inside two `overflow-hidden`, `z-[120]` boxes, so it was clipped mid-sentence and could
never rise above the flyout beside it. That module exists precisely because this app clips CSS
bubbles; not using it was the mistake.

Every flyout change routes through one setter, so an armed "Remove?" outlives everything short
of its own submenu closing.

# Smaller corrections

The composer chip separates model, effort and fast with dots rather than running the bolt onto
the effort. Effort tick marks are dark, 2px, and taller than the knob, so they read as the
delimiters they are. The stop button says "Interrupt agent" unless something is actually
queued, rather than promising to hand back messages that do not exist. The code field loses its
`CODE#STATE` placeholder, which described one lane's shape and misdescribed the others.

# Greet a new workspace

A workspace with nothing signed in cannot start a chat, so every affordance on the new tab is a
dead end until a provider is added. The chooser opens once, the first time that is true -- not
on reload, not on reboot, and not the next time the list happens to be empty, because someone
who removed their last provider has already met the screen. The chooser itself is half again as
big: it is the first thing a new workspace shows and it carries the decision the product hangs
on.

# Size each sign-in screen to itself

One 690x498 panel for every screen meant the "All set" confirmation -- a check, a heading and a
line of text -- sat in six hundred pixels of white. Each screen now carries BOTH its own width
and its own height ceiling: 440/320 for a verdict, 600/420 for a method list, 640/480 for a
form, 690/560 for the lane list.

Height is content-driven rather than set. The overlay centers rather than stretches, the body
has no height of its own, and the footer sits outside the scroll region -- so the panel is
exactly header + content + footer until the content passes that screen's ceiling, at which
point the body scrolls and the panel stops growing. Every ceiling is `min(px, vh)` so the
tallest screen still fits a laptop, and the entry screen's floor is clamped the same way: an
unclamped floor above the ceiling would push the panel off the bottom on a short viewport.

The entry screen is the one that keeps that floor, because the flow always returns there and a
panel that shrinks under the pointer on the way back reads as a fault.

The verdict screen is sized like the whole screen it is: a 64px disc, the heading at full
weight and full contrast rather than a step back, and the detail line in the body colour.

# Fix the model search field

It was the wrapper's class on a bare `<input>`, so it had a border but no icon and nothing
suppressing the browser's own focus ring -- the orange halo. The markup the stylesheet was
written for (wrapper, icon, borderless input) already existed; it just was not used.

# Grow the model list upward

The flyout stood at the row that opened it and was capped by the space BELOW -- which, for a
card that opens from the composer at the bottom of the panel, is about three rows. pi's
thousand-model catalog was being shown through that keyhole. It is now anchored at its base and
grows up, showing ten rows before it scrolls, with the search field beneath the list so the
edge nearest your hand stays put. A slim always-visible scrollbar says when there is more.

# Absorb PR #502's backend half

#502 is closed. Its frontend work was already here and further along; its parser work was not.

**A failed pi turn drew an empty bubble.** pi puts nothing in `content` when a turn fails and
the whole failure in a sibling `errorMessage`. The parser read text from `content` alone, so it
emitted `text: ""` -- an agent stuck on a rejected key looked exactly like one that had stopped
answering. The text now comes off `errorMessage`, gated on `stopReason: "error"` so a genuine
reply that quotes an error JSON is not styled as a failure.

**codex threw away why a turn died.** The reason arrives on `task_complete.error`, and that is
the ONLY durable copy -- codex classes its live `EventMsg::Error` non-persistent, so it never
reaches the rollout. We kept it on the turn marker for the auth flag and never showed it, which
meant it existed and was unreachable. It is now surfaced as an assistant message ordered before
the marker, classified off the structured `codex_error_info` tag where codex gives us one and
off the prose otherwise.

**One auth table, not two.** claude answered "is this an auth error?" from its own
`claude/auth_patterns.py` while pi, codex and antigravity used the shared `auth_errors.py`.
Folded in and deleted: claude's entries were never claude-specific -- a credit balance and a
proxy budget are facts about a billing relationship, not about a CLI. `error_patterns.py` moves
up alongside it and gains pi's bare `"<status> {json}"` surface form, anchored to the start of
the string so a status code quoted mid-message is not read as a failure.

**The two families cannot stack.** `classify_api_error` now returns None for anything the auth
vocabulary claims. They overlap by construction -- Anthropic reports exhausted third-party usage
as a 400 `invalid_request_error`, which is in both tables -- and a message carrying both
subtexts would offer two contradictory next steps.

**The auth subtext offers both ways out.** "Sign in again" re-authenticates this chat's own
account in place, which is the only thing that revives THIS conversation. "Switch to another
provider" opens the chooser and starts a fresh chat on whatever is picked, because a chat binds
to its account at creation and nothing rebinds it. Which one is right depends on whether the
credential is fixable -- an expired login is, a spent quota mostly is not -- and the user knows
that where we do not.

Signing in from the first-run greeting starts the chat rather than closing onto an empty new
tab. The greeting only fires when there is no provider at all -- the one state where the new tab
has nothing it can do -- so landing the user back there, having just signed in, sends them to
find the one button that was always the only thing to press.

# Phase 6: the chat and its terminal are one card

A chat and its agent's terminal are the same conversation rendered two ways, so they are now the
front and back of one surface. A Terminal switch in the composer's under-bar turns it over.

The under-bar moved OUT of `ChatPanel`'s footer to sit beside the card rather than on it. Inside,
the switch would rotate away with the face it turns and the flip would be one-way.

The back face mounts on the first flip and is never removed again. Mithril destroys a vnode that
becomes null, and destroying that one takes its iframe out of the document -- which ends the ttyd
session rather than hiding it. So the sticky flag is what is stored and `flipped` only drives the
transform. Neither face is ever `display: none`: ttyd sizes the agent's tmux window to its client
viewport (`window-size latest`), so a zero-sized back face hands the agent a zero-column terminal.
`backface-visibility` hides them at no layout cost, and `inert` keeps the hidden one from taking
focus or a click. `prefers-reduced-motion` drops the rotation and keeps the swap.

The card lives in its own module with its own tests. Those three rules are the whole risk of the
feature and every one of them is invisible in review; assembled inline they would be buried in
six hundred lines of transcript machinery.

# An agent terminal is no longer a tab

Not deprecated -- removed, along with everything that supported it. It was reachable four ways
and is now reachable one.

- The "Open agent terminal" button is gone, replaced in place by the switch.
- `chat-terminal:<name>` is REJECTED BY NAME in `layout.py`, and its prefix stays registered
  precisely so it can be: delisting it would send the ref to the bare-service-name fallback, and
  the caller would wait five seconds to be told that a service nobody mentioned is unregistered.
- Both frontend branches that opened and resolved that ref are deleted, as is the `split` op's
  permission for it.
- The server stops projecting a terminal panel back to `chat-terminal:<name>` and stops listing
  an `agent-terminal` entry per agent -- `layout list` would otherwise advertise a ref that
  `layout.py` now refuses.
- Layouts saved while it was still possible have their terminal panels dropped on restore,
  permanently rather than as a migration. Identified by the ttyd dispatch args in their URL, not
  by their `iframe-agent-<id>-<ts>` panel id: that id shape is shared with every iframe an agent
  opens through `llm-api.openTab`, so an id test would also delete app panes and ad-hoc URLs an
  agent had put there, on every view switch.

Side-by-side is given up deliberately. Two live ttyd clients on one tmux window keep resizing it
out from under each other -- `window-size latest` means the most recent attach wins -- so two
views of one terminal were never really two views.

# Two pi lanes stop sharing one tab name

Chat tabs count under a word chosen by harness, which is right until two lanes share one:
Opencode Go and OpenRouter both run on pi, so both fleets minted "Pi 1", "Pi 2", and the tab
strip could not say which provider a chat was spending. A lane may now carry its own word, and
only the two that collide do.

# Two documents that this branch made false

`test_workspace_claude_config.py` said every claude in a workspace resolves the shared
`~/.claude`. A chat agent does not: it runs against the account it was created against, with
`CLAUDE_CONFIG_DIR` set on its own process. Its assertions were still right -- what they actually
pin is that nothing pins that variable at a level everything else INHERITS, which is what keeps
the ambient default working for a bare `claude`, `claude_p.py` and the services -- so the
docstring now says that instead, and says why an exported value would silently put every chat
back on one shared credential.

`billing-and-credentialing.md` still described credentials living in one shared
`~/.claude/settings.json` written by a single sign-in modal. Corrected: a chat's credential lives
in its account folder, one env var per harness names it, and the shared path is now only the
ambient default a bare `claude` falls back to.

# Corrections

The Terminal View switch is the shared one from the combo card, unmodified. It was being scaled
down here with CSS that set `transform` on a knob whose offset already comes from a Tailwind
`translate-x-[22px]` utility; the utility won, and a 22px throw in a 32px track put the knob
outside it.

On a provider row, the tick is pinned to the right edge and the sign-out control sits to its
LEFT. The mockup slides the tick aside on hover to let the bin take the edge -- but the tick says
which provider this chat runs on, and that does not change because the pointer passed over the
row.

Removing a provider says what it actually does. It takes the credential off disk; it does not
reach into a process that already read it, so a chat already running can keep answering until it
next restarts. The old wording promised it "will not be able to take another turn", which is
false for exactly the case the user is looking at. Killing those agents to make the old sentence
true would be worse: it destroys a chat they may still be reading, to enforce a rule they can be
told.
