---
name: build-app
description: "Create a new app for the user -- a page, dashboard, or tool they can open as a window on the desktop. Use when the user asks for one, whether scaffolded as a new Flask app (the canonical path) or wrapping a pre-existing third-party server. For changing an existing app use update-app."
metadata:
  author: imbue
  crystallized: true
  exempt-checks: contents
  exempt-reason: awaiting-restructure
---

# How to build an app

An app is something the user opens as a window on the desktop: a supervisord
program with a manifest (`app.toml`) under `system/apps/`, rendered at its own
browser origin, `http://<name>.<workspace-host>/`. The forwarder routes that
origin straight to the port the app registers; nothing proxies or rewrites its
traffic. To change or remove an existing app, use the `update-app` skill.

## Standing instructions

- **Read `.agents/shared/references/interactive-delivery.md` first.** Building
  an app is that interactive shape, not "scaffold, implement, ship": confirm
  the look and feel on a cheap throwaway mock, build to a usable state in the
  chat, and leave thorough testing and the review gates to a background harden
  pass.
- **Build nothing real before the user confirms the mock.** Scaffolding is
  cheap and reversible; a data layer or a state architecture is not. This skill
  exists to prevent building, testing and hardening a whole app before the
  user has confirmed its basic shape.
- **Data from outside goes through `fetch-process-show` first.** If the app
  reads records from outside itself (an upload, an export, an API, a
  third-party service) and that skill did not send you here, stop and run it:
  an app whose ingestion was written here has no entry point anyone can re-run
  when the next batch lands. Its confirmed `sample.json` confirms the data's
  shape, not the UI's, so step 4 still runs, rendering the sample.
- **A mock is shown only inside the mock frame**, which labels it a "UI
  Sketch" and says what is not real yet. Never open the app itself
  (`verify_app.py --open`, `workspace-layout open <name>`) while any of it is a
  mock. To the user it is a UI sketch: never call it a mock in chat.
- **Check, then show.** A window is on the user's screen the moment `--open`
  or `workspace-layout open` returns. Do your own checks before that call, and
  never tell the user to open a window you opened.
- **The harden pass is never yours to run.** New test files, the full suite and
  the review gates are step 7's harden worker's.
- **Never extend `system/apps/system_interface/`** to add an app: it is the
  shell. A new app is its own package under `system/apps/<package>/`, with its
  own window and origin.
- **Ask in the user's terms**, who may not be technical: "should everyone see
  the same list?", never "do we need multi-tenancy?".

## Steps

- **Step 1: Clarify and plan.** *Ends the turn:* wait for approval.
- **Step 2: Choose the path, the name and the icon.** Wrapping an existing
  server replaces steps 3 to 5 with `references/wrap-existing-server.md`.
- **Step 3: Scaffold the app.**
- **Step 4: Confirm a mock.** *Hard gate; every round ends the turn* until the
  user explicitly confirms the look and feel.
- **Step 5: Build the real app**, framed until it is really hooked up.
- **Step 6: Verify, then show.** *Ends the turn:* ask whether the working app
  is right.
- **Step 7: Hand off to the harden pass**, only on the user's explicit
  confirmation of the working app.
- **Other tasks**, at the end: renaming or removing an app, its public URL,
  `forward_port.py`'s flags.

## Step 1: Clarify and plan

Ask only the questions that block: a fork both genuinely uncertain and
expensive to reverse. Most apps have none. Default to the simplest conventional
choice and to a **single user**, state each default in one line, and move on.
Choices that are cheap to reverse (persistence, auto-reload or
reload-to-refresh, latest-only or history) do not block: pick the obvious
default and let them surface in the mock loop or as a later follow-up, where
the user reacts to something concrete.

Record the defaults you stated: they are the architecture you build once, after
the mock converges. Build none of it yet. Propose a small plan and wait for
approval.

## Step 2: Choose the path, the name and the icon

- **Path.** Writing the routes yourself is the default: steps 3 to 5 scaffold
  a Flask app. For a pre-existing third-party server with its own start
  command (Jupyter, Grafana, an `npx`-installed dashboard), follow
  `references/wrap-existing-server.md` in place of steps 3 to 5 and rejoin at
  step 6. Never scaffold a Flask app whose only job is to shell out to such a
  tool: the forwarder already routes the app's origin to whatever URL is
  registered.
- **Name.** Kebab-case: lowercase letters and digits with single hyphens. It
  becomes the hostname label of the app's origin, so it must not start with
  `host-` or `agent-` (workspace hostname coordinates), and must not be the
  first label of a standalone service (`share`, `app`, `owner`, `vm`, `host`,
  `env`, `agent`, and `github` once GitHub sync is on), which would claim that
  service as the app's sidecar. Nor may it be a name an existing program uses
  (`system_interface`, `browser`, any `system/supervisord.conf.d/*.conf`); the
  scaffolder refuses those. Short and descriptive (`news`, `docs-viewer`) beats
  clever.
- **Icon.** Draw one specific to what this app does, to the rules in
  `docs/system/app-icons.md`: a 216 by 216 two-layer SVG tile with one
  fill-only glyph, coloured from that doc's palette. A brand-new registration
  is refused without one.

## Step 3: Scaffold the app

```bash
uv run --no-sync .agents/skills/build-app/python/build_app_skill/scaffold_flask_lib.py \
    --name <name> \
    --description "<one-liner>" \
    --icon-file <path-to-svg> \
    --start \
    [--display-name "<what users see>"]
```

`--description` is one line of at most 200 characters saying what the app is
for, in the user's terms: it is what an agent reads to decide whether the app
does what it needs. Pass `--display-name` (at most 64 characters) when the
description is too long to be the app's name. `--start` registers the app and
waits until it answers healthy. The port is picked for you, so do not check
ports by hand. A failure exits non-zero with a clear message.

`references/scaffold-output.md` has the other flags, what each generated file
is for, the program block, how the app starts, adding a dependency, and an app
that needs a credential. Read it when you change something the scaffold wrote
other than routes, or when a start fails.

## Step 4: Confirm a mock

Serve a throwaway mock of the proposed UI as a route of the scaffolded app, so
the user reacts to the real look and feel in a real window. Before you first
edit the app's code, read `references/app-code.md` (what every page keeps) and
`references/frontend-choices.md` (the design defaults).

- **A page, not a picture.** Never draw the screen as an image (an SVG, a PNG,
  a generated image) in place of the page: a picture cannot be clicked, so it
  shows none of the interactions the user is judging, and it is thrown away
  instead of growing into the real page. Only when the user asks for images,
  show them in the chat (the `show-files-in-chat` skill) and loop on them like
  any mock; once they confirm a look, build the served mock from it and show
  that.
- **Static content only**: no real fetching, no persistence, no backend logic.
  Render the confirmed `sample.json` if you were handed one; otherwise
  placeholder data covering the shapes the real app will show, including an
  empty state and a busy or overflowing one.
- **Still mostly frontend when feedback asks for a feature.** Show how it would
  look and roughly work, and confirm the user is happy with that before the
  heavy backend work.

Check the page, then frame it and open the frame beside the chat, in one call:

```bash
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<expected-heading-or-text>" --screenshot /tmp/<name>-mock.png && uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py up --app <name> --note "<what is not real yet>" --open
```

The note is one plain sentence in the user's terms naming what is not real:
whether the content is made up or a sample of their real data (and how much of
it), and which controls do nothing yet ("Showing 20 of your emails as a sample;
refresh and archive don't work yet.").

Then loop: take feedback, change the mock so the change is *visible*, show it
again. Never accept feedback and move on having only said you will apply it.
Each later round is the same call without `--screenshot`, ending in
`mock_frame.py refresh --app <name>` in place of `mock_frame.py up ... --open`:
every restart needs it, since a restart registers the app as an ordinary app
again. The frame's other commands (a new note, a closed window, taking it down)
are in `references/mock-frame.md`.

**Hard gate.** Loop until the user **explicitly confirms** the look and feel;
no real routes, data or state before that. The confirmed mock is the source of
truth for the UI's shape: if later work changes the look and feel, confirm
again before calling the app done.

## Step 5: Build the real app

Replace the mock with real routes and wire in the data and state decisions
recorded in step 1. The goal is a *usable* app the user can try, not a hardened
one.

- Read `references/api-schema.md` before adding a route: each owes the app's
  API document a docstring and a route-table entry ("The app describes its own
  API").
- `references/app-code.md` has the other rules: state under `DATA_DIR`, the
  content-security policy, a "view raw" control on derived data, calling
  Claude.
- `references/app-manifest.md` says when an app that works between requests
  needs `stop_when_no_windows = false`, and how to register what you build for
  the app outside its directory.

**The mock frame stays until the app is really hooked up.** Confirming the mock
does not take it down. The app stays framed while any of it is not real, above
all its data: an app showing a confirmed `sample.json` is still a mock until it
reads the real source (the crystallized pipeline's output, the live API),
however real its routes are. Each time a piece becomes real, say what still is
not in a new note ("Your real inbox loads now; archive doesn't work yet.").
Once the app reads its real data and does what the user will use it for, take
the frame down and open the app in its place. Both commands are in
`references/mock-frame.md`.

## Step 6: Verify, then show

One call restarts the app, checks and renders its page, and opens its window
beside the chat:

```bash
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<expected-heading-or-text>" --screenshot /tmp/app.png --open
```

- Already on the user's screen: `--refresh` in place of `--open`, and no
  `--screenshot`. Always refresh after a change, so the user sees it.
- Still framed: neither flag; end with a `mock_frame.py` command instead.
- The recipe's other forms and the manual checks are in `references/verify.md`.
  For anything unexpected (connection refused, a window stuck on the loading
  page, broken WebSockets), read `references/cross-flow-gotchas.md`, which is
  indexed by symptom.

Skip the window only for an app with no UI (a JSON API, a webhook receiver).
When the check was done another way, the window commands are
`uv run --no-sync workspace-layout open <name> --beside` (drop `--beside` only
for an app the user wanted running, not shown) and
`uv run --no-sync workspace-layout refresh --app <name>`. Everything else about
windows is the `manage-desktop` skill.

Then stop and hand the running app to the user: ask a plain "this generally
looks good?". Start the test gate `AGENTS.md` asks for through
`system/scripts/workspace_bare_scripts/run_in_background.py`: it is not the
harden pass, but the worker may never be dispatched.

## Step 7: Hand off to the harden pass

**The trigger is the user's explicit confirmation of the *working* app, never
your own sense that the code looks done.** The mock confirmed the shape; this
confirms the real behaviour, where deep changes surface, so an earlier pass
risks hardening an architecture the user is about to invalidate.

- A change request is a cheap foreground iteration (steps 5 and 6 again) that
  resets the clock. Hand off only when the reply is a confirmation.
- Cosmetic tweaks, or a pivot to a follow-up or an unrelated task, suggest the
  core is settled. Still ask, but ground it: "seems like we've got the core
  thing settled here -- good to lock it in?".
- Never hand off on silence or a timeout. The user is not blocked: they hold
  the usable app.

On confirmation, invoke the `crystallize-creation` skill with `type=app`. It
owns the rest: the tracking ticket, the task file (`creations: app:<name>`),
the harden worker, the poll, the merge on `done`, and the window refresh after
it. Give it the slug (the app's name) and a task body naming the app's package
path, its name, its URL segment and what it does. The app is on disk and
confirmed live, so the worker reconstructs nothing and has no gate of its own.
If the pass changes the look and feel, confirm with the user again before
calling the work done.

## Other tasks

- **Renaming an app**: `references/app-manifest.md`, "Renaming an app".
- **Removing an app**: the `update-app` skill, whose teardown steps for an app
  are `references/cleanup.md`.
- **The shared (public) URL** of an app: `references/public-url.md`.
- **`forward_port.py`'s flags**, for a registration written by hand:
  `references/forward-port-cli.md`.
- **The program schema and its logs**:
  `.agents/shared/references/service-processes.md`.
