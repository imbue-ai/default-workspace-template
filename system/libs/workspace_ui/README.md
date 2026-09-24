# workspace_ui

The workspace frontends' shared JavaScript library: what the shell
(`system/apps/system_interface/frontend`), the chat page
(`system/apps/chat/frontend`), and the Getting Started page
(`system/apps/getting_started/frontend`) have in common. Source only: there is
no build here, each app's vite build compiles the modules it imports
(`@imbue/workspace-ui/src/<module>`), and the four packages are one npm
workspace rooted at `system/package.json` (one `npm ci`, one lockfile).

- `src/base.css`: the design system's token layer (colour and type tokens, the
  typography roles, the base layer, the component keyframes). Each app's
  stylesheet imports it after `@import "tailwindcss"` and adds its own rules;
  `system/apps/system_interface/frontend/style_guide.md` is the rule for all of
  them.
- `src/components/`: the shared Mithril recipes (Button, Modal, NoticeDialog,
  the Menu, the Dropdown that picks a value, icons, badges, tooltips, the modal
  backdrop); beside it at `src/`, `DestroyConfirmDialog.ts`, `portal.ts`, and
  `menu-position.ts` (the pure geometry the Menu and the Dropdown place
  themselves with).
- `src/base-path.ts`, `src/origin.ts`, and
  `src/models/` (`ClientIdentity`, `http`, `backoff`, `ws-json`,
  `request-error`): the base helpers every page shares.
- `src/app_contract.ts`: an app page's side of the browser-side contract
  (contracts.md section 10 of the workspace app model, extended by section 7
  of the desktop interface's contracts.md), which the shell's frontend also
  builds into the module every app serves at `/_static/app_contract.js` from
  its own origin; `src/element_reference.ts`, `src/context_menu_rows.ts`, and
  `src/context_menu.ts`: the element context menu
  (`docs/system/blueprint/element-reference-menu/`): the JSON description of
  a right-clicked element, the menu's rows (the browser's own edit, link, and
  image rows, then "Copy path to element", "Explain this element...", and
  "Modify this element..."), and the installer every page runs, built into the
  module every app serves at `/_static/context_menu.js` with its framework-free
  renderer for pages without Mithril (`src/components/contextMenuOpener.ts` is
  the opener a Mithril page hands it, backed by the shared Menu); `src/embed.ts` and
  `src/embed-contract.d.ts`: the minds embed contract (the vendored source is
  aliased by each app's vite config); `src/terminalFocus.ts`: the focus grant
  the shell sends a framed page.
- `src/search.ts`: `matchesQuery`, the one text match of the workspace's
  typeaheads (every whitespace token of the query occurring in one of the given
  texts, case-insensitively), which the desktop's launcher, the Getting Started
  page's search, and the chat's send picker narrow their lists with.
- `src/testing/`: what a Mithril view test needs beyond jsdom, imported by every
  frontend's view tests: `dom.ts` (the `requestAnimationFrame` polyfill, imported
  before mithril) and `mount.ts` (`mountView` and `unmountViews`).

```bash
cd system && npm ci      # every frontend's dependencies
cd system && npm test    # every package's tests, this one's included
cd system/libs/workspace_ui && npx vitest run
```
