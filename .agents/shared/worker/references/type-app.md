# Type: app

An app -- a scaffolded Flask lib under `system/apps/<package>/`, registered by
its own `system/supervisord.conf.d/<name>.conf`, served at its own browser origin
(`http://<name>.<workspace-host>/`, routed straight to the app's registered
port). This reference describes what an app *is*; for how to
run and test a web frontend in isolation, see
`.agents/shared/worker/references/web-frontend-testing.md`.

## Where the source lives

An app's footprint is its own directory, the supervisord program blocks that
run it (each in its own `system/supervisord.conf.d/` drop-in), and the
`[[references]]` its `app.toml` declares -- skills, scripts, docs, and other
apps. It may be the only consumer of its references, or they may be shared.
The scope file (`harden-creation.md`) is that footprint resolved to literal
paths.

- The scaffolded lib: `system/apps/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`. This is the scope file's `primary`.
- The app's program entry at `system/supervisord.conf.d/<name>.conf` -- you
  normally do not touch it; the scaffold created it. The app is a standalone uv
  project with its own `uv.lock` and `.venv` (`uv add` in its directory changes
  its dependencies), so the root `pyproject.toml` and `uv.lock` need no entry.
- A background service co-owned by the app (a `<app>-<role>` program) also
  lives in the app's folder; treat it as part of the app, and its
  `[program:<app>-<role>]` block as one more `wiring` section beside the app's
  own.
- The manifest's `[[references]]`: the skills, scripts, and docs built for this
  app that sit outside its directory, each with a `note` naming the surface it
  uses. A change that lands outside the footprint -- `diff.outside_footprint` in
  the scope file names it -- is registered here when the file belongs to the
  app, rather than left unclaimed.

## Running and testing

The isolated-instance and rendered-page rules are in `web-frontend-testing.md`.
App specifics:

- A user-built app is a standalone uv project
  (`.agents/shared/references/running-python.md`, "User packages are standalone
  projects"): the worktree's setup syncs its venv from its own lock (`uv sync
  --locked` in its directory does it again), and a new dependency is `uv add
  ...` **in its directory**, committing its own `pyproject.toml` and `uv.lock`,
  never the root's. A built-in app is a root workspace member: a fresh worktree
  has no root `.venv`, so run `uv sync --all-packages` once before any `uv run`,
  and `uv add` at the root.
- Add a `test_<package>.py` for the routes. While you iterate, run a user-built
  app's suite from its own directory, against its own venv; the root pytest
  configuration skips user packages, so a run by path from the repo root
  collects nothing. The gate runs it from there too, so a test resolves repo
  paths from its own file (as the scaffolded `runner_test.py` does), not from
  the working directory. A built-in app's suite runs by path from the repo
  root, where the root pytest configuration applies and the app's repo-relative
  paths resolve (the shell's and the chat's suites are their own pytest roots,
  run from their own directories):

  ```bash
  (cd system/apps/<package> && uv run --no-sync pytest)   # a user-built app, plus test_<package>_ratchets.py
  uv run --no-sync pytest system/apps/<package>           # a built-in app
  ```

  Its pages import the shell's built modules, which a fresh worktree lacks
  (the shell's `static/` is gitignored), so build them once before its
  browser tests: `(cd system && npm ci && npm run build)`.

  The gate is `harden-creation.md`'s "The test gate": `select-tests` turns the
  app's `[[references]]` into the suites that exercise them (a referenced
  skill's tests run when the app's surface moves), along with everything else
  the change reaches.

## Working in isolation

Beyond the live-instance rules in `web-frontend-testing.md`: do not run `workspace-layout
open` / `refresh` / `list` against the served tree.

## Phone and sharing checks

The user built and confirmed the app on a desktop, as its owner, so two ways
of reaching it went untried: from a phone, and by someone else. Once sharing
is on, the app has its own public address (`build-app`'s "The shared (public)
URL"), and the user can open it from their phone's browser
(`.agents/skills/use-from-phone/SKILL.md`) or grant it to other people and to
visiting agents. The crystallize pass runs these checks after its first
milestone (`op-crystallize.md`, Stage 3). Each finding is either fixed and
covered by a test, or named in your report; none is a reason to stop the pass.

### On a phone

A phone shows one window at a time, filling the screen, or the app's own
link opened directly in its browser; either way the app's page gets a
phone-sized viewport and a finger instead of a mouse. Drive the isolated
instance (`web-frontend-testing.md`) in a browser context at 393x852 and at
852x393 with `has_touch=True` and `is_mobile=True` (without `is_mobile`,
Chromium ignores the page's viewport tag and does not draw what a phone
does), and check:

- The page carries `<meta name="viewport" content="width=device-width,
  initial-scale=1">`, without which a phone draws the desktop layout shrunk
  to fit.
- Nothing scrolls sideways at 393 wide
  (`document.documentElement.scrollWidth <= 393`; not `window.innerWidth`,
  which an emulated phone widens to fit a page that overflows): a wide table
  or code block scrolls inside its own container, and fixed widths give way
  to the screen's.
- Every action the desktop offers can be done by tapping. Nothing is reachable
  only on hover (a row's buttons that appear on hover, a tooltip that holds
  the only copy of a value), controls are big enough to tap (about 44px), and
  a menu or dialog fits on screen and closes.
- A text field's font is at least 16px, or iOS zooms the page when it gains
  focus.

The user confirmed the desktop layout, so a fix for a phone applies only on a
phone, never as a new desktop look: under the media queries the workspace's
own apps switch on (`system/libs/workspace_ui/src/device_queries.ts`), a
phone-sized viewport either way round, which covers 852x393 where a
`max-width` breakpoint does not, or `(pointer: coarse)` for what a finger
needs (hover-only controls, tap targets). Look at the screenshots at both
phone sizes and at the desktop size the user confirmed, as
`web-frontend-testing.md` requires for any page you change. Commit one
`browser` test in that phone context (393x852, `has_touch=True`,
`is_mobile=True`; `page.tap` fails without touch) that asserts no sideways
scroll and runs the app's main action by tapping it. The repo-root
`conftest.py` does not reach a user-built app's suite and its dev group has
no Playwright, so add `playwright` to that group (`uv add --dev playwright` in
its directory), register the `browser` marker in its
`[tool.pytest.ini_options]`, and launch Fortress by its path inside the test,
as `AGENTS.md` says.

### When it is shared

Over a share, every route answers whoever holds a grant on the whole
workspace or on this app (`system/services/share_gateway/README.md`,
"Grants"), and a visiting agent scoped to it ("Visiting agents" there), not
just the pages the user clicks. Each request carries `X-Imbue-Identity`
("Request identity" there): `owner` is always present; a person over a share
adds `user_id` and `email`; a visiting agent adds `agent_id`, `agent_name` and
`granter`, with no `user_id` ("Visiting agents", "Admission"); and a request
with no header is the owner's, which is safe only from loopback under a
loopback `Host`. Every built-in app installs a guard for that and the scaffold
does not, so add `install_request_guard(app, exempt_rules=frozenset())` from
`app_schema.request_guard` right after the app is created, ahead of every
other request hook (`system/libs/app_schema/README.md`; `exempt_rules` names a
route meant for pages of other origins). It answers 403 to a page of another
origin and to a header-less request from anywhere else. A header that does
not parse is never read as the owner's: `parse_identity_header` raises on it
and the guard answers that with a 400, so a route that reads the identity
answers 400, and a route that never reads it does not see the difference.
Read the identity with `workspace_layout.identity`, as the shell and the chat
app do; it comes with `app-schema`. Use `parse_identity_header`,
`is_owner_participant`, and `participant_key_of`, which names the requester
`user:<id>`, `agent:<id>`, or `owner`. Locally the header looks the same, so
the test client exercises all of this without turning sharing on.

- **The API.** Go through every route the app's API document lists and decide
  what a visitor may do with it. Reading what the app shows is usually what
  sharing is for. Look harder at a route that reveals a secret, a credential,
  or the owner's data beyond what the pages show; spends the owner's money (a
  Claude call, a paid API); changes or deletes data; or runs a command or
  touches files outside the app's own data. A route that hands a visitor a
  secret or a credential is a defect: make it owner-only, answering a visitor
  403 the way the chat app's `system/apps/chat/imbue/chat/identity.py` does.
  Whether visitors may see the owner's data beyond what the pages show,
  change, delete, spend, or run a command or touch files outside the app's
  data is the user's call, not yours: unless the task file says, keep the
  behavior and name each such route in your report. The API
  document is what a visiting agent reads to learn the app (`build-app`'s
  "The app describes its own API"), so a route's description says when it is
  owner-only.
- **Who did what.** When the app stores something a person or an agent made
  or changed (an edit, a submission, a comment), store the requester's
  participant key with it; `email` and `agent_name` are for display only,
  never the key.
- **The access log.** When visitors can change anything, the app keeps a log
  of who did it: one line per request that changed something, with the time,
  the participant key, whether it was the owner, and the route. Keep it under
  the app's data directory and bound it like any other store ("Bound disk
  growth" in `harden-creation.md`). Log reads too only when the app shows data
  the owner would want to know was seen. The share gateway's own access log
  (its README, "The access log") records every request but never who made it,
  so it does not replace this one.

Cover each with a test through the test client, sending a person's header
(`{"owner": false, "user_id": "...", "email": "..."}`) and a visiting agent's
(`{"owner": false, "agent_id": "...", "agent_name": "...", "granter": "..."}`):
an owner-only route answers both 403 and still works for the owner and for a
request with no header, a route that reads the identity answers 400 to a
header that does not parse, a change records the visitor's participant key,
and the access log gains its line.
Assert the guard with `app_schema.testing`'s
`assert_request_guard_refuses_unknown_provenance` on one of the app's reads.

## Critical apps

The shell (`system/apps/system_interface`), the chat (`system/apps/chat`), the
terminal (`system/apps/terminal`), and any app whose `app.toml` says
`critical = true` reach you only through the careful flow's handoff
(`op-update.md`, "Critical-app handoff"): the lead built the change in a
worktree, previewed it to the user, and handed you the branch at approval.
What differs for you:

- **Build every bundle at the npm workspace root** (`cd system && npm ci && npm
  run build`), never one frontend alone: the shell's, the chat's, and the
  Getting Started app's bundles are built from the shared
  `system/libs/workspace_ui/` library, and the apply installs the bundles you
  built only when it is given all three. Report each `static/` path in your
  `done` body, so the lead can pass them to the apply.
  The test gate selects its own checks from what changed; building every
  bundle is for the apply, whatever the gate ran.
- **Never drive `networkidle`** against a shell or chat instance in Playwright:
  both hold sockets open for as long as they run, so the wait never returns.
  Wait for the element you are about to read instead.
- **Verify against your own instance.** Boot it with `preview_app.py`
  (`.agents/skills/update-app/python/update_app_skill/preview_app.py up --app <name> --worktree
  <your work_dir>`) or the raw isolated-instance script; the live app and its
  windows are the user's.
- The go-live is the lead's: the atomic update apply, after your `done`. You
  neither merge nor restart anything in the served tree.
