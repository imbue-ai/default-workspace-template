# workspace_ui

The workspace frontends' shared JavaScript library: what the shell
(`system/apps/system_interface/frontend`) and the chat page
(`system/apps/chat/frontend`) have in common. Source only: there is no build
here, each app's vite build compiles the modules it imports
(`@imbue/workspace-ui/src/<module>`), and the three packages are one npm
workspace rooted at `system/package.json` (one `npm ci`, one lockfile).

- `src/base.css`: the design system's token layer (colour and type tokens, the
  typography roles, the base layer, the component keyframes). Each app's
  stylesheet imports it after `@import "tailwindcss"` and adds its own rules;
  `system/apps/system_interface/frontend/style_guide.md` is the rule for all of
  them.
- `src/components/`: the shared Mithril recipes (Button, Modal, NoticeDialog,
  menus, icons, badges, tooltips, the modal backdrop) and `DestroyConfirmDialog`,
  `portal.ts`, `flyout-position.ts`.
- `src/base-path.ts`, `src/origin.ts`, `src/addresses.ts`, `src/views.ts`, and
  `src/models/` (`ClientIdentity`, `http`, `backoff`, `ws-json`,
  `request-error`): the base helpers every page shares.
- `src/app_contract.ts`: an app page's side of the browser-side contract
  (contracts.md section 10), which the shell also builds into the module it
  serves at `/_static/app_contract.js`; `src/embed.ts` and
  `src/embed-contract.d.ts`: the minds embed contract (the vendored source is
  aliased by each app's vite config); `src/terminalFocus.ts`: the focus grant
  the shell sends a framed page.

```bash
cd system && npm ci      # every frontend's dependencies
cd system && npm test    # every package's tests, this one's included
cd system/libs/workspace_ui && npx vitest run
```
