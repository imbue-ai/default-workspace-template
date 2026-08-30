# Share button deep link

Make the workspace's Share action open the Minds app's real share page for the
app it was clicked from, replacing today's instructional popup.

## Overview

- Today the Share action opens `ShareModal`, a static card telling the user to go
  open the Minds app and find workspace settings. It is a dead end: it explains
  where to go instead of taking you there.

- The destination already exists and needs no new UI. The Minds options overlay's
  Share tab accepts `?target=<serviceName>`, and `ShareModel.selectTarget` focuses
  that service. This change is a link to it.

- The share controls stay in the Minds app rather than being rebuilt inside the
  workspace. The share API is served by the Minds app, whose address the workspace
  cannot know (its backend listens on a random per-run port), so asking the Minds
  app to open the page is both the simple option and the only workable one.

- All postMessage traffic is confined to the embed contract, enforced by ratchets
  in both repos. A new destination therefore means a new contract type, not an
  ad-hoc message.

- Follow `openPermissionRequest`'s fire-and-forget shape exactly: send and return.
  No ack, no timeout, no fallback. This deliberately differs from
  `openImbueMintPage`, which does ack (see Open questions).

- The work spans two repos on separate release cadences. mngr owns the contract and
  the handler; default-workspace-template owns the click.

## Expected behavior

- Clicking Share on an app tab's three-dots menu opens the Minds app's Share page,
  already focused on that app, with its enable-sharing control. Clicking Share on
  a sidebar app row does the same thing.

- A well-shaped service name the Minds app does not recognize falls back to the
  whole-machine share (`selectTarget`'s existing behavior for an unknown target).
  A malformed `serviceName` payload is dropped by the contract validator before
  any handler runs, so no machine-level share entry point is added: every Share
  click names a service.

- The workspace iframe is not torn down or reloaded. The options route is an
  overlay the Shell floats over the still-mounted workspace surface.

- A share deep link that arrives while the options overlay is already open still
  selects the named target: the panel applies `?target` whenever the param's
  VALUE changes, not only at model load. An unchanged param never re-selects, so
  the URL cannot fight the user's own target navigation (which does not touch
  the URL).

- The instructional popup is gone entirely.

- When the workspace is opened directly in a browser rather than through the Minds
  app, Share does nothing. This matches the permission card's "Review & respond"
  button today.

- Between the default-workspace-template change landing and the next vendor sync,
  a Minds app that predates this contract version ignores the message and Share
  does nothing. The contract's tolerant policy makes this a silent no-op rather
  than an error.

## Implementation

### mngr

**`apps/minds/imbue/minds/desktop_client/static/embed_contract.js`**

- Bump `CONTRACT_VERSION` from `"2"` to `"3"`.
- Add `OPEN_SHARE_SETTINGS = "minds:open-share-settings"` to the workspace ->
  embedder type block. Payload `{ serviceName }`, required.
- Add `SERVICE_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/`: a conservative superset
  (with a length cap) of the registry's own service-name rule
  (mngr_latchkey's `_SERVICE_NAME_PATTERN`, `^[a-z0-9][a-z0-9_-]*$`).
- Add a `WORKSPACE_TO_EMBEDDER_VALIDATORS` entry requiring a well-shaped
  `serviceName`, shaped like `OPEN_REQUEST_MODAL`'s requestId validator.

**`apps/minds/docs/embed-contract.md`**

- Version 2 -> 3, inventory row, version-history entry.

**`apps/minds/frontend/src/views/shell/WorkspaceFrame.ts`**

- Add `OPEN_SHARE_SETTINGS: string` to the `EmbedContractModule` interface.
- Register the handler in `buildEmbedHandlers`: navigate to
  `/workspace/<agentId>/options` with `{ tab: "share", target }`. No exported
  re-validation helper: the contract endpoint validates before dispatch, so the
  handler only type-narrows, exactly as `OPEN_AI_KEYS_PAGE` does for hostId.
  (The value also never reaches a URL path -- it rides route query params and
  ends at `selectTarget`'s known-targets check.) No ack.

**`apps/minds/frontend/src/views/pages/WorkspaceOptionsPage.ts`**

- Replace the load-time-only `?target` application with an exported
  `applyRequestedTarget(share, appliedTarget)` reconcile run every render:
  applies the param once the share model exists, and re-applies only when the
  param's value changes. This is what makes a deep link that lands on an
  already-open panel work.

**Tests** -- `WorkspaceFrame.test.ts` (handler routes to the Share tab with the
target, sends no ack; tolerates an absent name by landing untargeted) and
`WorkspaceOptionsPage.test.ts` (`applyRequestedTarget`: holds the param until
the share model exists, re-selects only on value change, consumes a dropped
param without selecting).

### default-workspace-template

**`system/apps/system_interface/frontend/src/embed-contract.d.ts`**

- Declare `OPEN_SHARE_SETTINGS` and `SERVICE_NAME_PATTERN`, keeping the
  hand-written mirror faithful to the upstream module.

**`system/apps/system_interface/frontend/src/embed.ts`**

- Export `OPEN_SHARE_SETTINGS` via the namespace-probe-with-literal-fallback
  already used for `PERMISSION_REQUEST_RESOLVED` (the type postdates the
  vendored snapshot; a named import of a missing export fails the build). No
  new module: embed.ts is the declared single connection to the embedder and
  already owns this pattern.

**`system/apps/system_interface/frontend/src/views/DockviewWorkspace.ts`**

- Both call sites (`shareAction.run`, `shareMemberRow`) become a direct
  `sendToEmbedder(OPEN_SHARE_SETTINGS, { serviceName })`. The modal import,
  its module state, its render block, and the `m.redraw()` calls go away.

**Deletions** -- `views/ShareModal.ts` and the orphaned `.share-modal-*` rules
in `style.css` (including pre-existing orphans from an older richer modal).

**Changelogs** -- one entry per repo under `changelog/preston-share-deeplink.md`.

## Testing strategy

- Unit coverage as listed above; no workspace-side unit test for the send
  itself (a two-line `sendToEmbedder` call, same as `openPermissionRequest`).
- The postMessage-confinement ratchets in both repos stay green by
  construction (`sendToEmbedder` is the sanctioned path).
- Typecheck, lint, and full suites in both repos.
- Manual verification: `just sync-vendor-mngr-live`, boot a workspace, click
  Share on an app tab and on a sidebar app row; confirm the Share tab opens
  focused on that service without the iframe reloading.

## Open questions

- **Fire-and-forget leaves a dead button with no Minds app present.** Deliberate,
  matching `openPermissionRequest`; `openImbueMintPage` acks instead. Whether
  both buttons should gain embed detection is a later, joint decision.

- **Whole-machine share visits are the concrete case where that bites.** Sharing
  the whole machine exposes this same dockview UI top-level, where app tabs
  still show a Share item that does nothing. Not addressed here.

- **`CONTRACT_VERSION` is documentation, not a wire field.** Bumping it is what
  the module's own comment asks for; skew detection would be a separate change.
