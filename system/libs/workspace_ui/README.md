# workspace_ui

The workspace frontends' shared JavaScript library: what the shell
(`system/apps/system_interface/frontend`), the chat page
(`system/apps/chat/frontend`), the Getting Started page
(`system/apps/getting_started/frontend`), Files
(`system/apps/files/frontend`), and Routines
(`system/apps/routines/frontend`) have in common. Source only: there is
no build here, each app's vite build compiles the modules it imports
(`@imbue/workspace-ui/src/<module>`), and the six packages are one npm
workspace rooted at `system/package.json` (one `npm ci`, one lockfile).

- `src/base.css`: the design system's token layer (colour and type tokens, the
  typography roles, the base layer, the component keyframes). Each app's
  stylesheet imports it after `@import "tailwindcss"` and adds its own rules;
  `system/apps/system_interface/frontend/style_guide.md` is the rule for all of
  them.
- `src/components/`: the shared Mithril recipes (Button, Modal, NoticeDialog,
  the Menu, the Dropdown that picks a value, the SegmentedControl, icons,
  badges, tooltips, toasts, the modal backdrop); beside it at `src/`, `DestroyConfirmDialog.ts`, `portal.ts`, and
  `menu-position.ts` (the pure geometry the Menu and the Dropdown place
  themselves with).
- `src/base-path.ts`, `src/origin.ts`, and
  `src/models/` (`ClientIdentity`, `http`, `backoff`, `ws-json`,
  `request-error`, `failure`, and `toasts`, the queue `components/toasts.ts` draws): the base helpers every page
  shares.
- How a failed request is told (`models/failure.ts`, `models/http.ts`,
  `models/request-error.ts`): one reader, `readFailure`, says what went wrong,
  for the REST helpers (`HttpError.failure`) and for a mithril rejection
  (`describeRequestFailure`) alike: a route's `user_facing_message` (a sentence
  for people, reportable only when the body says `reportable: true`), else its
  `detail` or `error` (a reason, developer text), else no answer (nothing
  answered, or a gateway status 502, 503 or 504 with no JSON reason), else
  nothing. `failureSentence(failure, whatFailed)` is the one place that becomes
  words, `whatFailed` being a sentence stem ("This window couldn't be closed"):
  the server's sentence as written; no answer as "The workspace didn't answer.
  Try again in a moment." for a read and "The workspace didn't answer, so
  Imbue Studio can't tell whether this went through." for a change, never
  prefixed with what failed (and never "nothing was changed": Imbue Studio's
  forward proxy can send a gateway status after the request arrived); a refusal
  (4xx) with a reason as "<what failed>."; anything else as the fallback "<what
  failed>. Try again. If this persists, report a bug.". A screen not yet worded
  this way shows `failureText` (an `HttpError`'s `message`, and
  `describeRequestError`): the server's own reason, else our sentence. The status and the
  server's own text go behind "See details" (`src/components/SeeDetails.ts`,
  which `NoticeDialog` draws from its `details`).
- A failure's two buttons, both through the shell that frames the page
  (`src/components/FailureButtons.ts`): `AskAgentToFixButton` starts a chat
  that asks an agent to fix the failure (`askAgentToFix` in
  `src/app_contract.ts`, one task text for every caller: what failed, the
  details verbatim, and what to do), and renders nothing where no shell frames
  the page; `ReportBugButton` asks the shell to open Imbue Studio's bug report
  (`shell:report-bug`), and renders only where the shell's handshake said
  `canReportBug` (the embedder's `opensHelp`, which only the shell hears; it
  hands every page a fresh handshake when that changes). Each reads the
  connection as it renders, so the page redraws on a handshake. The shell's own
  toasts open the report directly (`src/report-bug.ts`).
- `src/app_contract.ts`: an app page's side of the browser-side contract
  (contracts.md section 10 of the workspace app model, extended by section 7
  of the desktop interface's contracts.md), which the shell's frontend also
  builds into the module every app serves at `/_static/app_contract.js` from
  its own origin, and which follows the link clicks and script popups of a
  page a shell frames (its handshake has arrived) by one rule
  (desktop-interface contracts.md section 7, "Link rule"),
  so no app carries link code of its own. A page without Mithril (the
  terminal's and the browser's server-rendered pages) builds its failure
  buttons from the same module, in a `<script type="module">` (a dynamic
  `import()` works too: the shell greets a page again when it connects, even
  after its frame's `load`):

  ```js
  import { connectToShell, askAgentToFix } from "/_static/app_contract.js";

  const connection = connectToShell({ onHandshake: showFailureButtons });
  function showFailureButtons() {
    askButton.hidden = !connection.hasShell();
    reportButton.hidden = !connection.canReportBug();
  }
  askButton.onclick = () =>
    askAgentToFix(connection, { whatFailed: "The terminal couldn't start", details: output });
  reportButton.onclick = () => connection.reportBug();
  ```

  `src/element_reference.ts`, `src/context_menu_rows.ts`, and
  `src/context_menu.ts`: the element context menu
  (`docs/system/blueprint/element-reference-menu/`): the JSON description of
  a right-clicked element under a random `REF-<id>`, the menu's rows (the
  browser's own edit, link, and image rows, then "Copy reference",
  "Explain...", and "Modify..."), and the installer every page runs, built into the
  module every app serves at `/_static/context_menu.js` with its framework-free
  renderer for pages without Mithril (`src/components/contextMenuOpener.ts` is
  the opener a Mithril page hands it, backed by the shared Menu); `src/embed.ts` and
  `src/embed-contract.d.ts`: the Imbue Studio embed contract (the vendored source is
  aliased by each app's vite config); `src/terminalFocus.ts`: the focus grant
  the shell sends a framed page.
- `src/themes/`: a page's side of workspace themes
  (`docs/system/blueprint/workspace-themes/`): `parts.ts`, the contract's part
  names and the attributes that mark an element as one; `themeClient.ts`, which
  wears the theme the shell names (`shell:theme`) and remembers it unless it is
  a preview; and
  `themeBoot.ts`, the Vite plugin whose inline script wears the remembered theme
  before a page's first paint. The themes themselves are folders under
  `system/themes/`, not part of this library.
- `src/page/`: the page kit, built by the shell's frontend into
  `/_static/workspace_theme.css` and `/_static/workspace_theme.js`, which a page
  built as plain HTML loads to wear the workspace's theme.
- `src/links.ts`: what a link means in the workspace
  (`blueprint/workspace-link-routing/` in the mngr repo): `classifyLink` calls a
  link external, a file (a `file:` URL on this machine), a local URL, one of
  this workspace's app addresses
  (an address on its share domain included), another workspace's, or
  unroutable, and `externalLinkMessageType` names the message an external link
  becomes (`open:web`, or `open:<scheme>`: `open:mailto`, `open:tel`,
  `open:slack` for a custom-scheme link). The shell classifies every link
  handed to it with it, and the chat every link of a message it renders; its
  test holds its external and custom-scheme rules (the app contract's
  `isExternalUrl` and `isCustomSchemeUrl`) to the desktop app's (the vendored
  `link-externality-vectors.json`).
- `src/download.ts`: `startDownload`, how the workspace's own pages download a
  file of their own origin: by loading it in a hidden frame rather than through
  a `download` link, whose request the browser sends without the workspace's
  session cookie when Imbue Studio frames the page, and reporting what the
  server said instead when it answers with something other than a file.
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
cd system && npm run lint && npm run format:check && npm run typecheck   # eslint, prettier, tsc
cd system/libs/workspace_ui && npx vitest run
```
