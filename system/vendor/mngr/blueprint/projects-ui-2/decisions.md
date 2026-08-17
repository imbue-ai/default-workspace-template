# Projects UI — settled decisions

The authoritative design is `spec-projects-and-docked-tabs.md` (+ the seven
screenshots beside it), supplied by Preston. Where anything in this repo
disagrees with that spec, the spec wins.

`plan-projects-ui-2.md` predates the spec and is **superseded** wherever the
two conflict; the decisions below are the reconciliation.

## Answers that override earlier ones

| Question | Decision | Supersedes |
|---|---|---|
| Membership | **Explicit**, not implicit. A project owns a durable list of member refs (`service:<name>`, `service:browser?session=<n>`, `chat:<agent-id>`, `terminal:<name>`, `url:<hash>`) separate from its layout. A member with no panel is *backgrounded*, still listed in the sidebar. Closing a tab never stops the underlying service. | The shipped implementation, where membership was implicit in the saved layout |
| Sharing | **Many-to-many. No partition at all.** An object may appear in any number of projects. There is no owner and no "move" -- only add-to-this-project and remove-from-this-project. | Both the earlier "apps only" answer and the later "strict partition" answer |
| Everything | The **unfiltered view, with its own layout**. It is the *home*: every object on the machine appears in it, including objects in no project at all. Removing something from a project never removes it from Everything. | The interim "lens with no dock" answer, which was premised on jumping to an object's owning project -- meaningless once nothing owns anything |
| Migration | An existing machine upgrades into **one starter project containing everything**. | Earlier scoping of "template-only, no migration needed" |

## Objects are singletons; views only decide where they are shown

There is **one live page per object**, machine-wide. An app open in three projects is one
iframe and one document, not three — a project is only a view that may or may not include it,
and a pane is only a place to show it at some size.

This is the browser-tab mental model, and it settles several things at once:

- Typing into an app, then switching to a project that does not include it, **preserves the
  page**. It is still running and still holds its state; nothing is showing it.
- Switching to a project that includes it **in a different position or at a different scale**
  shows *the same page*, resized to that pane. It does not reload and it does not fork.
- Therefore the live element for an object must **never leave the DOM** — not on a view
  switch, not on a close, not on a re-arrange. Removing an iframe from the document destroys
  it, and re-parenting one reloads it, so the element has to stay mounted and be
  positioned/sized to whichever pane currently displays it.
- A design that keeps a separate dock per view and rebuilds panels inside it is therefore
  **wrong**: it would give the same app one document per view. Whatever holds the live
  elements has to be global and keyed by ref, with the per-view layout deciding only
  geometry and visibility.

## The model, stated plainly

The machine holds one pool of objects (chat agents, terminals, browsers, apps,
url tabs). A **project is a filter over that pool plus its own layout** — a
*view*, in Preston's words, or a lens. **Everything is the view with no
filter**, and it too has its own layout.

- An object appears in Everything always, and in each project whose filter
  includes it. Being in several projects at once is normal, not an exception.
- Adding an object to a project adds it to that project's member list.
- **"Remove from project" removes it from that project's view only.** It keeps
  running and still appears in Everything and in any other project holding it.
- Nothing needs a home project, because Everything is the home.
- Destroying an object is the only thing that takes it out of Everything, and
  it drops out of every project at the same time.

## Pinning an app to a project IS membership

There is one concept, not two. An app is either **pinned in a project** or it is
not, and that is the same fact as whether the project's member list holds its
ref -- there is no second, separate pin state.

This replaces an earlier arrangement where "pinned/unpinned" was a per-browser
localStorage preference layered on top of membership, which produced four
states (pinned/unpinned x in-project/not-in-project) that had to be reasoned
about together and disagreed across browsers.

- The rail's app shortcuts are the project's pinned apps, in member order.
- The "All apps" popover has exactly two headings: **"Pinned in <Project>"** and
  **"Unpinned"**. The first is this project's app members; the second is every
  other app on the machine.
- The only two verbs are **pin to this project** (add the member) and **unpin
  from this project** (remove it). Unpinning never stops the app: it is the same
  act as removing any other object from a view.
- Because it is membership, pinning is server-side and shared: it follows the
  project rather than the browser it was done in.
- Everything is the unfiltered view and pins nothing -- every app on the machine
  already shows there.

## Other settled answers

- **Deleting a project** stops its services, confirm-gated, with the dialog
  explicitly enumerating what will be stopped (so §10's "nothing should be
  *silently* killed" is honored by disclosure rather than by survival). Under
  the many-to-many model the dialog must not offer to stop anything that
  another project still holds.
- **Cross-project open** from the launcher's "On this machine" table **adds**
  the object to the current project. Nothing leaves the project it was in.
- **Chat membership rides the mngr agent `project` label** (§2) rather than a
  parallel bookkeeping list, so child agents inherit it. With many-to-many
  membership that label is the chat's *originating* project, not an exclusive
  owner, and the project member list is authoritative for what a view shows.
- **Objects in no project at all are fine.** They surface in Everything.
- **Project settings** (name, color, glyph, delete) keeps the modal already
  built, but is reached from the **sidebar switcher header's context menu**,
  not a separate top-bar control.
- **"Open in new window" is deferred** entirely (§9's mechanism question is
  unresolved); no tear-off, no menu item that pretends to work.
- **File Viewer** needs a real file-viewer app built for the workspace
  template; the shortcut stays unwired until it exists.

## In scope for this pass

1. Explicit membership model + backgrounded objects.
2. Full sidebar: switcher header, shortcuts, All apps (with per-project
   pin/unpin), search pill, and the member tab list with per-row kebab.
3. Everything as a lens.
4. New Tab launcher replacing both the "+" dropdown and the empty-state
   overlay; the dock never goes empty.
5. Connected tab-card visual treatment (§1).
6. Equal-width tabs (§5).
7. Tooltip restyle to match mngr-internal's `.minds-tooltip` (§8).

## Deferred

- Open in new window / tear-off (§9).
- The file-viewer app itself (tracked separately from wiring its shortcut).
- Per-type context-menu item sets beyond what §6 already fixes; §6 says each
  object type needs its own pass against real lifecycle verbs.

## Carried over from the shipped PR (#400) — still valid

- Server-side project store: one JSON per project plus a registry holding
  display metadata and the last-active id. Extends to carry the member list.
- Stable project id with a separately editable display name; renaming never
  re-slugifies the id (§10 asks for exactly this split).
- Per-project squiggle glyph + color identity, glyph set lifted verbatim from
  the prototype with its render math calibrated to it.
- 37px collapsed rail, 150ms hover expand, labels fading in.
- Autosave debounce, content-hash echo guard, remote-apply suppression, and
  the teardown-before-seed ordering in the dockview layout apply.

## Known open items not yet decided

- Dark-mode canvas color (§1 says TBD).
- Whether the 4px inter-group gap and the tab API affordances need a
  dockview-core version bump (§ intro).
- How projects interact with the mobile layout (§10).
