---
name: update-app
description: "Use immediately whenever the user asks to update, change, fix, restyle, extend, restart, or otherwise modify an existing app or background service, before touching its code. Covers apps (a window the user can open) and background services (host-backup, share-gateway, and other supervisord programs with no window), the workspace's own critical apps (the shell, the chat, the terminal) included. For creating a brand-new app use build-app."
metadata:
  author: imbue
  exempt-checks: depth contents
  exempt-reason: awaiting-restructure
---

# Changing an existing app or service

Apps and services both run as a `[program:<name>]` under supervisord, one
program per file under `system/supervisord.conf.d/`:

- **App**: the user opens it as a window, rendered at its own browser origin.
  Its code is under `system/apps/<package>/`.
- **Service**: a program with no window (`host-backup`, `share-gateway`,
  forwarders), standalone under `system/services/` or co-owned by an app
  (named `<app>-<role>`, code in the app's folder).

Two things are easy to forget, and both leave the user looking at stale state:
a code change does not take effect until the process is reloaded, and an open
window keeps showing the old page until it is refreshed. The live change loop
(steps 3 to 5: make the change, apply and verify it, show it) handles both.

## Route the change first

**Before anything else, find the code the change touches and read the
`app.toml` of the app that owns it** (`system/apps/<package>/app.toml`).

- **A critical app, or a library the critical apps share: follow the careful
  flow, `references/critical-app.md`, for the whole change, in place of the
  steps below.** It sends you back here only for what it shares: the standing
  instructions, step 2's lease and step 3's list of what a change owes. That
  is an app whose manifest says `critical = true` (the shell
  `system_interface`, the chat, the terminal, and any user app that declares
  it), or a change under `system/libs/workspace_ui/` (the library both critical
  bundles are built from), `system/libs/workspace_layout/` (the layout wire
  contract the shell, the chat and the terminal all run) or
  `system/libs/workspace_themes/` (the theme catalog and theme route the same
  three run). A critical app is never edited in the served tree. When the
  change also touches an ordinary app or a skill, those go through that flow
  with it.
- **A new app**: the `build-app` skill.
- **Arranging windows** (open, place, focus, minimize, close): the
  `manage-desktop` skill.
- **Removing an app or service**: the last section.
- **An app still framed as a mock** (`workspace-layout list` lists
  `<name>-sketch` in its place): its window is the frame's, so wherever a step
  below says `--refresh` or `--open`, end the call with a `mock_frame.py`
  command instead, from
  `.agents/skills/build-app/references/mock-frame.md`.

## Standing instructions

- **Verify before the user can see it, not after.** Your checks belong where
  you are the only one looking: before the window exists, or against a
  throwaway instance of your own. Once a window is open the user is the
  verifier for anything they can perceive. Driving an app they are watching
  lands your test actions in their view and, for anything wired to real data
  or real agents, in their state.
- **A window is on the user's screen the moment `workspace-layout open`
  returns.** Run it only on something you are ready for them to see, and never
  follow it by telling them to open the window.
- **The store is the user's real data.** Reading `data/.apps/<name>/` is fine.
  Never let a check or a preview write to it, and never "clean up" by deleting
  from it. A check that must write runs against a copy, and a change that must
  itself rewrite the store (a data migration) takes a snapshot first: both are
  in `references/verify-against-a-copy.md`.
- **Never restart an app with `supervisorctl restart`.** It waits out the
  program's start window (30 s for a scaffolded app); step 4's call does not.
- **The harden pass comes after the user's feedback, in the background.** Never
  dispatch it in the turn you made the change, and never run the thorough
  tests or the review gates yourself.

## Steps

- **Step 1: Size the change.** A larger-scope change is confirmed on a preview
  first. *Each preview round ends the turn.*
- **Step 2: One editor at a time.** Take the editing lease.
- **Step 3: Make the change.**
- **Step 4: Apply and verify it.** For an app, one call that also does step 5.
- **Step 5: Show it, and end the turn.** Refresh or open the window, say what
  changed, release the lease. *Ends the turn.*
- **Step 6: Harden the change**, only once the user is satisfied.
- **Removing an app or service.**

## Step 1: Size the change

- **Larger scope**: a change that is costly to redo if its shape is wrong. A
  redesign, a new page, a meaningful shift in look and feel, a new user-facing
  capability, a change that touches the user's data. Read
  `.agents/shared/references/interactive-delivery.md` and run the mock-confirm
  flow `build-app` used to create the app: put a cheap, throwaway **preview**
  of the proposed change in front of the user, loop until they **explicitly
  confirm** the shape, and only then build it for real. Never build heavy
  against an unconfirmed shape.
- **Contained**: everything else, which for an ordinary app is most changes. A
  bug fix, a copy tweak, a new field, a backend logic fix, a config or
  `command` change, a restart. No preview: if the change *works*, a taste
  mismatch is cheap to fix next round, so it goes live and iterates. Go
  straight to step 2.

When in doubt, a change that alters what the user *sees or perceives* is larger
scope, and one that only alters behaviour behind an unchanged surface is
contained.

The preview takes whichever form is cheaper and still convincing (the two
demonstrative-prototype types of `interactive-delivery.md`):

- a throwaway mock page, the fastest for a look-and-feel loop;
- the changed app itself, booted beside the live one as the labeled
  `<name>-preview` app, when a mock page will not convince (a redesign, a
  change that touches data). `references/verify-against-a-copy.md`, "A preview
  from a worktree", has the commands.

A new page or capability added to an existing app is its own delivery with its
own feedback gate: confirm and ship it on its own, not bundled with unrelated
changes.

## Step 2: One editor at a time

Every chat shares one working tree and one live process, so two agents editing
the same app or service at once interleave destructively, with no merge step to
reconcile them. Edits are serialized by an advisory lease: a regular `tk`
ticket, which every agent can see.

1. Check whether another agent is mid-edit:

   ```bash
   tk ready > /tmp/service-leases.txt
   grep "editing service <name>" /tmp/service-leases.txt
   ```

   If a lease exists and `tk show <id>` says it is not yours, do **not** go
   ahead silently. Tell the user another chat is changing it and let them
   decide: wait, or override. If the holder looks abandoned (its agent is
   no longer running, or the lease is hours old with no notes), say so and
   offer to break it. A lease is broken on the user's call, never silently.

2. Take your own:

   ```bash
   LEASE_ID=$(tk create "editing service <name>" -t chore \
       -d "Held by $MNGR_AGENT_NAME while editing this service; released at turn end.")
   ```

   then `tk start "$LEASE_ID"`, as its own command.

**Release it at the end of every editing turn** with
`tk close "$LEASE_ID" "Done editing for this turn."`, and take a fresh one
before editing again. Never hold it across a wait for the user. Between turns
the changes are committed, so only *simultaneous* editing needs the lease.

**An in-flight harden pass does not block you.** If `tk ready` also shows an
in-progress `update` or `heal` ticket whose title lists `<name>` among its
creations, go ahead: your change makes that pass stale. Leave a note on its
ticket (`tk add-note <id> "..."`) so its owner catches the pass up at merge
time. The contention rules are in
`.agents/shared/references/harden-contention.md`.

## Step 3: Make the change

Edit the code under `system/apps/<package>/`, or wherever the program's
command points. Three things the change may owe:

- **The manifest.** Anything the change creates for the app outside its own
  directory is registered under `[[references]]`. If the change gives the app
  work between requests, or takes it away, `stop_when_no_windows` changes
  with it. Both rules are in
  `.agents/skills/build-app/references/app-manifest.md`; the restart in step 4
  is what applies a manifest edit.
- **The API document.** For an app with an `api_schema_path`, each new or
  changed route owes a docstring and a route-table entry, and the app's own
  test (`runner_test.py` in a scaffolded app) checks them, so run it with the
  change. The contract is `.agents/skills/build-app/references/api-schema.md`.
- **Claude.** If the change calls Claude, follow the `use-ai-integration`
  skill.

## Step 4: Apply and verify it

**An app, after a change to its Python code.** One call restarts the program,
waits for its port, checks a marker unique to your change, and then does step
5:

```bash
uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<text unique to your change>" --refresh
```

- With no window open yet, pass `--open` in place of `--refresh`. The window
  opens only after the checks pass, and before it does you may add
  `--screenshot <path>` for a rendered page.
- When the change is an API, add one `curl` of the changed route to the same
  call.
- A frontend-only change (templates, static files served fresh on each
  request) needs no restart: drop `--restart`.
- The recipe, and the manual checks for a call that fails (connection
  refused), are `.agents/skills/build-app/references/verify.md`. Symptoms in
  the window (stuck on the loading page, broken WebSockets) are in
  `.agents/skills/build-app/references/cross-flow-gotchas.md`.

**Anything else** is in `references/applying-a-change.md`: a dependency or
entry-point change, a service with no port, a change to the program's
definition, and a restart of the whole services agent.

**What you may check depends on who is looking.** With no window open, check
freely, as a user would and not just "the process is up". With the app already
on the user's screen, the call above is the whole check, plus the kind that
cannot touch their view: a `curl`, a health probe, an exit code, a log line.
The thorough pass is the harden worker's, on its own instance.

## Step 5: Show it, and end the turn

For an app, step 4's call has already refreshed or opened the window. By hand,
that is `uv run --no-sync workspace-layout refresh --app <name>`, which reloads
every page of the app on every client, or
`uv run --no-sync workspace-layout open <name>` when no window is open.
Anything else about windows is the `manage-desktop` skill. A service has no
window.

Tell the user what changed, release the lease, and end the turn.

## Step 6: Harden the change

**Get the user's feedback before any harden pass.** Delivering a change is not
the same as the user *wanting* it. This holds for every change, a one-line copy
tweak included, and it can take **several rounds**: treat each reply as another
iteration of steps 2 to 5 and hold the pass until you are sure the user is
satisfied. A single "looks fine" while they are still tweaking is not done. For
a larger-scope change the gate is the *working* result, not the preview.

Then invoke one skill, once, for the whole change:

- **`update-creation`**, for a change you applied live or repeatable work you
  did by hand. List every creation it touched: each app as `app:<name>`, each
  service as `service:<package>`, each skill the same change edited as
  `skill:<name>`. Two apps changed to work together are one pass with one
  worker. It opens the ticket, dispatches the harden worker on its own branch,
  proxies the gates, merges, and refreshes each window on go-live.
- **`heal-creation`**, when the app or service errored or gave a wrong result
  and you worked around it. Again once, covering every creation the failure
  spans.

Both enforce one pass per creation: if another chat has a pass in flight for
any creation in yours, they leave a note on its ticket, and that pass catches
up at merge time to cover both changes
(`.agents/shared/references/harden-contention.md`).

## Removing an app or service

For a service, follow "Removing a service" in
`.agents/shared/references/service-processes.md`. For an app, follow
`.agents/skills/build-app/references/cleanup.md`, which also drops its
registration and its package.

Teardown stops at the code and the process. **Leave the data
(`data/.apps/<name>/`) in place**: removing an app or service is not license to
delete the user's records. Delete the data directory only if the user
explicitly asks, and confirm before you do.
