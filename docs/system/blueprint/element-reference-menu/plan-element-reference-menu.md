# The element reference menu

This is the spec for letting a user point at anything on the screen and hand it to a chat: a right-click on any element of the desktop, of a built-in app, or of an app an agent built opens a context menu whose last rows attach a description of that element, an *element reference*, to a chat's message as a `REF-<id>.json` file with a short prompt that names it ("Explain what I attached in REF-<id>", "Change REF-<id> to "), or copy it to the clipboard.
An agent reading the chat then knows exactly which element the user meant, in terms it can resolve against the source (tag, id, classes, data attributes, a selector) and against the workspace (the app, the window, the page path), without the user describing "the font under the list near the image".
It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
It builds on the desktop interface V1 ([plan-desktop-interface.md](../desktop-interface/plan-desktop-interface.md), [concepts.md](../desktop-interface/concepts.md), [contracts.md](../desktop-interface/contracts.md)) and the post-launch-paths plan ([plan-post-launch-paths.md](../post-launch-paths/plan-post-launch-paths.md)), whose chat intake carries every draft, and amends those documents where section 15 says.

## 1. Purpose and principles

A reference is resolvable, not unique.
Nothing in this design mints an identifier for an element: what a page already carries (its `id`, its `data-*` attributes, the identity class each element keeps first in its class string) is captured as it is, and the scope the shell already keeps (the app, the window id, the page path) is added around it.
An agent resolves a reference by grep and by reading the page, which is what it does with a file path; it does not look anything up in a registry.
The one id minted is the reference's own, `REF-<id>`, a random name for the attachment the reference becomes, so a message can call it by name; it says nothing about the element.

A reference is an attachment.
A chat already lets a user attach a file to a message, shows it as a chip in the composer, names its path on the message, and lets the agent open it; a reference takes that path whole, as a `REF-<id>.json` file, rather than a second one of its own.
The file holds everything the page captured, however long the user's selection was, and the composer holds one line naming it.

Every app draws its own menu.
The shell cannot see a click inside a page (a page is a cross-origin frame), and an app will want its own rows beside the reference rows, so the menu is the page's, built from a shared library that gives it the standard rows and the reference rows.
The shell's part is one message: a page hands it text to draft, and the shell drafts it into the chat the way "Design your own..." already does, naming no app.

The menu replaces the browser's.
A page that opens its own menu on `contextmenu` loses the native one, so the library's menu carries the rows the native one would have given (Cut, Copy, Paste, Select All, the link and image rows) beside the reference rows.
The reference rows show for every element, editable fields, selections, and images included; an agent can make sense of any of them.

The five principles of the workspace app model hold: one owner per fact; the shell is generic and names no app; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.

## 2. Glossary

| Term | Meaning |
|---|---|
| Element reference | The JSON description of one element on one page, with the scope the page knows (section 3.1) |
| Reference id | The reference's random name, `REF-<11 base-36 characters>`, minted when it is built; the message calls it by this |
| Reference block | The text a reference travels as until a chat attaches it: a fenced `json` block holding the reference (section 3.2) |
| Reference file | The attachment a reference becomes when it enters a composer: `REF-<id>.json`, uploaded as any file the user attaches (section 3.2) |
| Standard rows | The rows the native menu would have offered: the edit rows, the link rows, the image rows |
| Reference rows | "Copy reference", "Explain...", "Modify..." |
| Element menu | A page's context menu: the page's own rows, then the standard rows, then the reference rows |
| Draft | Text put into a chat's composer, unsent, through the chat's `draft` launch path (the post-launch-paths plan); the composer attaches any reference block in it |

## 3. The model

### 3.1 The element reference

A reference is one JSON object under one top-level key, `element_reference`, whose fields are `snake_case` like every JSON body in the workspace.
Every field is present; a fact the page does not know is `null` (a scalar) or empty (a list, an object, a string that is naturally empty such as a text).

| Field | Value |
|---|---|
| `reference_id` | `REF-` and eleven random base-36 characters, minted when the reference is built: the file's name and the word the message calls it by |
| `app` | The app the page belongs to, from the shell's handshake (section 5); `null` on a top-level visit |
| `window_id`, `desktop_id`, `client_id` | From the handshake; `null` on a top-level visit |
| `page_origin` | `location.origin` |
| `page_path` | `location.pathname` plus `location.search` |
| `page_title` | `document.title` |
| `viewport` | `{"width", "height"}`: the page's inner size in CSS pixels |
| `pointer` | `{"client_x", "client_y", "page_x", "page_y"}`: where the right-click landed, in viewport and in document coordinates |
| `tag` | The element's tag name, lower case |
| `id` | The element's `id`, or `null` |
| `classes` | The element's class list, in order, every class kept |
| `attributes` | Every attribute but `class` and `id` (carried above), by name, `style` included |
| `role` | The `role` attribute, or `null` |
| `aria_label` | The `aria-label` attribute, or `null` |
| `selection_text` | The document's selected text at the click, or `""` |
| `selection_box` | `{"x", "y", "width", "height"}`: the client rect of the selection's range, or `null` with no selection |
| `input_value` | The value of an input, textarea, or select, else `null`; a password field's value is never carried (the reference reaches the clipboard, a message, and a file), so it is `null` there too |
| `link_href` | The `href` of the nearest enclosing anchor, or `null` |
| `image_src` | The `src` of an image element, or `null` |
| `selector` | A CSS selector that matches exactly this element in the document (section 3.1.1) |
| `bounding_box` | `{"x", "y", "width", "height"}`: the element's client rect |

Nothing is truncated: a selection of any length arrives whole, since the reference travels as a file (section 3.2).
The element's own text, its markup, and its ancestors are deliberately not carried: they are large, and they locate nothing the id, the classes, the attributes, and the selector do not.

#### 3.1.1 The selector

The selector is built from the element upward: at each level the element's `id` when it has one (`#id`, then stop), else its tag with its classes (`div.message-user.selected`), with `:nth-of-type(n)` appended (`n` the element's index among its same-tag siblings) when another sibling has the same tag and the same classes.
The result is checked against the document with `querySelectorAll`; a selector that matches more than one element, or none, is replaced by `null`.
The check is what makes the field trustworthy; the construction is only a good first try.

### 3.2 The reference block and the reference file

A reference travels as text until it reaches a chat, because the shell and the intake carry text.
The block is:

````
```json
{"element_reference": {...}}
```
````

with the JSON on one line.
When a block enters a chat's composer (section 7.2), the chat takes it out of the text and attaches it as a file: `<reference_id>.json`, holding the envelope pretty-printed, uploaded through the same `POST /api/uploads` every attachment takes, stored under `data/uploads/<uuid>/` as they are, shown as a chip in the composer, and named on the sent message's "See attachment here:" line by its absolute path.
The agent runs in the same container as the chat app, so the path on the message is a path it can read, and it reads it as it reads any attachment.
The text left in the composer is the prompt that names the reference.

Only a composer entry makes a file, because only the chat app uploads: Copy reference on any page copies the block, whatever its size (section 4.3), and a block pasted into a composer is attached the same way when it enters through a draft.

### 3.3 The rows

A page's element menu is, in order: the page's own rows for what was clicked, when it has any (section 4.4); a divider; the standard rows the target admits; a divider; the reference rows.
A divider is drawn only between two non-empty groups.

The standard rows, each present only when it applies:

| Row | Present when | Does |
|---|---|---|
| Cut | The target is editable and text is selected in it | Copies the selection to the clipboard and deletes it |
| Copy | Text is selected (editable or not) | Copies the selection |
| Paste | The target is editable and the browser exposes `navigator.clipboard.readText` | Inserts the clipboard's text at the caret |
| Select All | The target is editable | Selects the field's whole value |
| Copy link address | The target is in an anchor with an `href` | Copies the absolute URL |
| Open link in new window | The target is in an anchor with an `href` | `window.open(href)` |
| Copy image address | The target is an image with a `src` | Copies the absolute URL |

The reference rows, always present:

| Row | Does |
|---|---|
| Copy reference | Puts the reference block on the clipboard |
| Explain... | Drafts `Explain what I attached in <reference_id>`, a blank line, the reference block |
| Modify... | Drafts `Change <reference_id> to ` (a space to type after), a blank line, the reference block |

"Editable" means an `input` (but not one of the button-like types), a `textarea`, or an element inside a `contenteditable` region.
A row that fails at run time (the clipboard refused) reports the failure to the console; nothing else changes.

### 3.4 Where a draft goes

| Surface | The draft goes |
|---|---|
| The shell's own chrome (title bars, the taskbar, the backdrop, the launcher) | `DesktopStore.draftText` (section 6) |
| A chat page with a composer (`/<chat-id>`) | Straight into its own composer, no shell round trip |
| A chat root (`/`, the rail and the frame around the selected chat) | Into the selected chat's composer; with none selected, `shell:draft-text` |
| A sub-agent view (`/<chat>.<agent>.<session>`) | `shell:draft-text`, relayed by the chat root when it frames the view |
| The Getting Started page | `shell:draft-text` |
| An agent-built app | `shell:draft-text` |

`shell:draft-text` is the new contract message (section 5); the shell answers it with `DesktopStore.draftText`, which runs the pinned app's draft launch path (the chat's `draft`, `target = current_chat`, `is_draft = "true"`) into this client's view of the pinned window, so the draft lands in the chat that window shows, else the most recently messaged chat, else a new provisional one, exactly as "Design your own..." lands.

Every draft is prepended above whatever the composer already holds, separated by a blank line (`prependToComposer`), so a message the user was typing survives and the prompt line sits on top; the reference block in it is attached first (section 7.2), so only the prompt is text.
Two references drafted before a send are two chips and two prompt lines, each naming its own.

### 3.5 Ownership

| Fact or verb | Owner |
|---|---|
| What a reference holds, and the selector rule | The library's `element_reference.ts` |
| Which standard rows a target admits, and what each does | The library's `context_menu_rows.ts` |
| Opening a page's menu and drawing it | The page: the shell, each built-in, and each agent-built app, through the library |
| Turning a draft text into a launch of the pinned app's draft launch path | The shell's store |
| Which chat receives a draft, and holding it until the root applies it | The chat app's intake (the post-launch-paths plan) |
| Attaching a reference as a file, and showing its chip | The chat app: the frontend helper every composer entry runs, over the upload path every attachment takes |
| Serving the library's modules from an app's origin | The shell and each agent-built app, at `/_static/app_contract.js` and `/_static/context_menu.js`; a built-in that draws the menu bundles the library from source |

### 3.6 Invariants

- The shell names no app and reads nothing inside a draft text: `shell:draft-text` carries opaque text, and the shell's part is the launch it already knows how to make.
- No file but the library's boundary modules touches `postMessage` or a `message` listener; the embed ratchets of the shell and the chat hold with no new allowlist entry.
- A reference is built from the DOM as it is; no attribute is invented for it and no page is required to carry any.
- A reference reaches the agent whole, as a file; a composer never holds a truncated reference, and never holds one as text.
- The menu yields to a page's own `contextmenu` handling: an event already `defaultPrevented` opens nothing.

## 4. Behaviour

### 4.1 Right-click on a page

The library's installer listens for `contextmenu` on the document.
An event that is already `defaultPrevented` is left alone (section 4.4 says how a page's own menus join in instead).
Otherwise the installer prevents the default, reads the target (the event's target element; a text node's parent), the selection (`getSelection().toString()`), and the click position, builds the reference (section 3.1) from those and the page's last handshake, and opens the menu at the pointer.
The menu closes on a press outside it, on Escape, and after a row runs; the shared Menu component's rules for the built-ins, the same rules in the framework-free renderer for agent-built apps.

The reference is built when the menu opens, not when a row runs: the selection and the element are what they were at the click, whatever the menu's own rendering does to them.

### 4.2 The standard rows

The edit rows act on the target: Cut and Select All through the field's own API (`setRangeText`, `select`) for inputs and textareas and through `document.execCommand` for a `contenteditable` region; Copy through `navigator.clipboard.writeText` with the selection text; Paste through `navigator.clipboard.readText` into the caret.
Paste is offered only where `readText` exists, since one browser family withholds it from pages; nothing is offered that cannot run.
The link rows resolve the `href` against the document, so a relative link copies as an absolute URL.

### 4.3 The reference rows

Copy reference writes the full reference block to the clipboard, however large, on every page: only the chat app attaches, and it attaches only where a draft enters a composer (section 7.2).
Explain and Modify hand the draft text of section 3.3 to the surface's draft route (section 3.4).

### 4.4 A page's own menus

A page that already opens a menu on `contextmenu` for some element (the taskbar's entries, the shortcuts, the desktops widget, the chat rail's rows) keeps that menu and appends the reference rows to it after a divider, through the library's `elementReferenceRows`.
It prevents the default as it does today, so the generic listener does nothing for that click; the standard rows are not added to such a menu (an entry or a rail row is not editable and holds no link).
Those menus therefore capture their target: the element the event fired on rides beside the click position into the menu's rows.

### 4.5 Nested frames

The chat root frames chat pages and sub-agent views of its own origin.
A chat page drafts into its own composer, so nothing crosses the frame.
A sub-agent view has no composer and posts `shell:draft-text` to its parent; the root's relay forwards it to the shell unchanged, as it forwards `shell:focused` and `shell:open`, since the shell is the only party that can turn it into a draft.
The root's own rail menu drafts into the selected chat's live page through the root's `draftInto` (the same-origin embed API), and with no chat selected posts `shell:draft-text` itself.

### 4.6 Touch and keyboard

On a coarse pointer the browser fires `contextmenu` on a long press, which is the whole of touch support in this design.
The menu's rows are buttons; Escape closes it; nothing else is added for the keyboard.

### 4.7 A page visited at top level

A page visited outside the shell (a share of one app, a developer's tab) has no handshake: the reference's `app`, `window_id`, `desktop_id`, and `client_id` are `null`, and the standard rows and Copy reference work as they do framed.
Explain and Modify are present but disabled, with the tooltip "Open this page in the workspace to draft into a chat", on a surface whose draft route is the shell's (section 3.4) when no shell frames the page; a chat page drafts into its own composer and keeps them enabled wherever it is visited.
A `contextmenu` event raised from the keyboard carries a `(0, 0)` position; the target is still the focused element, and the menu opens at the position given.

## 5. The contract

Two additions to the app contract (contracts.md section 7), both additive; shipped types are unchanged.

| Direction | Type | Payload |
|---|---|---|
| shell to page | `shell:handshake` | gains `"app"`: the name of the app the frame's window belongs to; a page reads a missing one as `""` |
| page to shell | `shell:draft-text` | `{"text"}`; the shell drafts the text into the chat the pinned app's draft launch path names, as "Design your own..." does (the post-launch-paths plan section 4.3); with no app on the machine taking a draft the shell notifies and does nothing |

`connectToShell` returns `draftText(text)` beside `startWithText(text)`, and `ShellHandshake` gains `app`.
The shell accepts `shell:draft-text` only from a frame it created, as it accepts every `shell:` message.

## 6. The shell

- `pages/livePages.ts`: the handshake carries `app` (the page's window's app); a `shell:draft-text` handler, `takeDraftText`, mirrors `takeStartWithText` and calls `store.draftText`.
- `store/DesktopStore.ts`: `draftText(text)`: when `draftTargetOf` names a pinned window on the active desktop that takes a draft, `draftIntoPinnedWindow(text)` runs there (a launch the shell refuses ends the draft; it is not retried elsewhere); with no such window, the first free-text row whose launch path declares a `draft_param` runs with the text through `runFreeText`; with none, the user is told `NO_DRAFT_APP_REASON`, "No app on this machine can take a draft".
- `views/App.ts`: the generic listener of section 4.1 on the shell's own document, opening a sixth kind of the desktop's one menu, `element`, whose rows are the standard rows and the reference rows for the captured target; the entry, shortcut, and desktop menus append the reference rows after a divider (section 4.4).
  The window and desktops menus open from a button, not a right-click, and are unchanged.
  The shell's draft route is `store.draftText`.
- The context menu callbacks of `TaskbarEntry`, `FloatingEntries`, `ShortcutIcon`, `DesktopsWidget`, and the views that thread them gain the event's target element beside the click position.
- `server.py` serves `/_static/context_menu.js` beside `/_static/app_contract.js`, with the same permissive CORS header, for the e2e stub pages.

The shell's reference for its own chrome has `app` `"system_interface"` and `window_id` the id of the window whose chrome was clicked when the target sits inside a `[data-window-id]`, else `null`.

## 7. The chat app

### 7.1 Backend

Nothing new: the reference file goes up `POST /api/uploads` as any attachment does, and is served, deleted, and stored as one.

### 7.2 Frontend: attaching

`models/elementReferences.ts` exports `stageElementReferences(chatId, text)`: it finds every fenced `json` block in `text` whose JSON parses to an object with the one key `element_reference` holding a reference (`referenceEnvelopeOf` checks the fields the chat reads: a well-formed `reference_id`, the tag, id, classes, app, and page path; a block without them is left in the text), stages each as an attachment of the chat's composer through the attachment store (a `File` named `<reference_id>.json` holding the envelope pretty-printed, with the reference's one-line summary for the chip), and answers the text with the blocks removed and the blank lines that set them off collapsed.
A text with no reference block is answered unchanged and stages nothing.

It runs inside `prependToComposer`, the one door every draft enters the composer by: the root applying a pending intake or drafting from its rail (through the page's embed API when the page is loaded, else this document's own copy), a chat page drafting from its own menu, and a block handed back by a sibling view.
The attachment store persists its ready items to localStorage beside the draft text, so a chip survives a reload as the text does, and a page finds what the root staged for it before it loaded (a `storage` event brings a list another document of the origin wrote into this one).
A reference's chip wears the pointer glyph and says what it points at (`referenceSummaryOf`: the element as `tag#id.classes`, then the app and the page) where a user's file says its size.

### 7.3 Pages

- The chat page installs the generic listener with the library's Mithril menu, drafting into its own composer; a sub-agent view installs it with `connection.draftText`.
- The chat root installs it for the rail and the space around the frame, drafting into the selected chat through `draftInto`, else `connection.draftText`; the rail's row menu appends the reference rows.
- The root's relay forwards `shell:draft-text`.

## 8. The Getting Started page

Installs the generic listener with the Mithril menu and drafts through `connection.draftText`.

## 9. The library

In `system/libs/workspace_ui/src/`:

- `element_reference.ts`: `ElementReference`, `describeElement(target, click, scope)`, `uniqueSelectorFor(element)`, `mintReferenceId()`, `referenceFileNameOf(id)`, `referenceBlock(reference)`, `referenceFileText(envelope)`, `referenceSummaryOf(reference)`, and `referenceEnvelopeOf(json)`.
  Pure over the DOM: no framework, no message primitive.
- `context_menu_rows.ts`: `ContextMenuTarget` (the element, the selection text, the click), `standardContextMenuRows(target)`, `elementReferenceRows(reference, draft, isDraftAvailable)` (Explain and Modify grey with the section 4.7 tooltip when the last is false), the prompts (`explainPromptOf(id)`, `modifyPromptOf(id)`), and the clipboard helpers.
  Its row type is structurally an `ActionRow` or a `DividerRow` of the shared Menu, so the built-ins pass the rows straight to `createMenu` and the framework-free renderer draws the same objects.
- `context_menu.ts`: `installElementContextMenu({connection, handshake, scope?, draft?, isDraftAvailable?, extraRows?, open?, document?})`, the one implementation of section 4.1: the document listener, the target and selection capture, the reference, and the rows.
  `handshake` is a getter for the page's last handshake, `null` before one; `scope(target)` replaces the scope the handshake gives, for a page that knows more than its handshake says (the shell, section 6); `draft` defaults to `connection.draftText` and `isDraftAvailable` to `connection.isFramed`, and a page that gives both (the shell, whose route is its store and which no shell frames) gives no `connection`; `extraRows(target)` puts a page's own rows first; `open(rows, point)` replaces the renderer; `document` is the document to listen on, the page's own by default.
  The default renderer is framework-free, for pages without Mithril: a fixed card of buttons with inline styles, closed by a press outside, Escape, or a pick.
  It returns an uninstaller, and a second install on a document already carrying its marker is a no-op that answers the first's uninstaller.
  Built as a second library entry of `vite.contract.config.ts` into `_static/context_menu.js`, bundling the two modules above and nothing else.
- `components/contextMenuOpener.ts`: `createContextMenuOpener()` answers an `open` for the built-ins, backed by `createMenu` and rendered through a render root of its own under `<body>`, so every Mithril page draws the shared Menu without a slot for it and none repeats the capture logic.
- `components/menu.ts`: the sheet under an open menu and the menu's card prevent the default of a `contextmenu` event, so a right-click that closes a menu, or lands on one of its rows, is a handled event the installer yields to (section 12) and the browser's own menu stays away; nothing else changes.

`app_manifest.registry` gains `SHELL_CONTEXT_MENU_PATH` and `CONTEXT_MENU_ROUTE` beside the contract's; the shell (section 6) and each scaffolded app (section 10) serve the module at that route, while the built-ins that draw the menu bundle the library from source and go on serving only the contract module.

## 10. Agent-built apps

The scaffold (`.agents/skills/build-app/scripts/scaffold_flask_lib.py`) changes in two places:

- A route `/_static/<basename>` serving `app_contract.js` and `context_menu.js` from the shell's static directory (the two names only; anything else is `404`), with the same mimetype the built-ins use.
- The index page's inline beacon becomes a module script that connects to the shell, reports its location on the handshake, and installs the context menu:

```html
<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>
```

The build-app skill says to keep both on every page the app serves, and adds a short markup convention: give a list row the `id` or a `data-*` attribute of the record it shows, and give an interactive element a stable `id` or identity class, so a reference resolves to one thing.
Nothing enforces it.
Apps built before this change keep their beacon and get no menu until they adopt the script; the update path is theirs.

## 11. The minds desktop app

Electron's main process pops a native menu on every `context-menu` event of the window's web contents, which the workspace frame shares.
It now pops one only for the main frame (the chrome page's own fields), so a right-click inside the workspace frame reaches the page's own menu alone.
Plain-browser mode needs nothing: the native menu is what the page's `preventDefault` replaces.
This is the one change outside the template, in the mngr repository's `apps/minds/electron/context-menu.js`.

## 12. Errors and edge cases

- A right-click on the shell's shield over an unfocused window raises the window and opens no menu, as any press there does; the next right-click reaches the page.
- A right-click while the shell's launcher or a menu is open lands on the shield or the sheet and closes them; no menu opens.
- The clipboard API rejects (a page without focus, a denied permission): the row reports it and the menu closes.
- `shell:draft-text` with no pinned draft target and no draft row: the notice of section 6, nothing else.
- The upload fails (the chat app is restarting): the chip shows "Upload failed" as any attachment's does, the send refuses and names it, and the user removes it or tries again; the prompt stays in the composer.
- A reload between the right-click and the send: the chip comes back with the text, from the persisted list.
- A `contextmenu` event with no element target (the document itself): the target is `document.documentElement`.
- A page that installs the listener twice (a script included twice): the second install is a no-op, keyed on a marker the installer sets on the document.
- An element removed from the document between the click and the row (a redraw): the reference was built at the click and is unaffected; the edit rows act on the field they captured, which does nothing when it is gone.

## 13. Testing

- `element_reference.test.ts`: the reference of an element with an id, with data attributes, of a repeated row (the `:nth-of-type` and the uniqueness check), of an editable field (`input_value`, and `null` for a password field), of a link and an image, of a text node target, and on a top-level visit; the reference id, the file name, the file text, the summary, and the block.
- `context_menu_rows.test.ts`: which standard rows each kind of target admits; the draft texts of the reference rows; Paste absent without `readText`.
- `context_menu.test.ts`: the installer opens on `contextmenu`, yields to a `defaultPrevented` event, closes on Escape and on a press outside, runs a row, and installs once.
- `app_contract.test.ts`: `draftText` posts `shell:draft-text`; the handshake carries `app`.
- The shell: `livePages.test.ts` (the handshake's `app`, `shell:draft-text` reaching the store), `DesktopStore.test.ts` (`draftText` with a pinned draft target, with a draft row only, with neither), `App.test.ts` (a right-click on the backdrop opens the element menu with the reference rows; an entry menu ends with them).
- The chat: `relay.test.ts` forwards `shell:draft-text`; `elementReferences.test.ts` attaches each reference block as a `REF-<id>.json` file with its summary and leaves other blocks alone; `ComposerAttachments.test.ts` persists ready attachments, restores them, and takes in a list another document wrote.
- The scaffold: the runner page imports both modules and installs the menu; the static route serves the two files and refuses others.
- The minds app: `context-menu.test.js` shows no menu for a subframe event.
- Manual: in a workspace, right-click the desktop backdrop, a taskbar entry, a chat message, a chat rail row, a Getting Started tile, and a page of a freshly scaffolded app; Explain puts a `REF-<id>.json` chip and a prompt naming it into the chat on screen each time, the sent message names the file, and the agent opens it and finds the element.

## 14. Implementation order

1. The library: `element_reference.ts`, `context_menu_rows.ts`, `context_menu.ts`, the contract additions, the second build entry; their tests.
2. `app_manifest.registry` constants; the shell serves the second module.
3. The shell: handshake `app`, `takeDraftText`, `draftText`, the element menu and the appended rows; tests.
4. The chat: the attaching helper, the persisted attachment store, the page menus, the relay; tests.
5. Getting Started.
6. The scaffold and the build-app skill; tests.
7. The minds app's Electron change.
8. Documents (section 15) and changelog entries.

## 15. Amendments to existing documents

- `desktop-interface/contracts.md` section 7: the handshake's `app` field and the `shell:draft-text` row; the `connectToShell` return gains `draftText`.
- `desktop-interface/plan-desktop-interface.md` section 7: the same in brief.
- `system/apps/system_interface/README.md`: `shell:draft-text` beside `shell:start-with-text`; the element menu.
- `system/apps/chat/README.md`: how a reference becomes an attachment.
- `system/libs/workspace_ui/README.md`: the three modules.
- `system/libs/app_manifest/README.md`: the second served module.
- `docs/system/README.md`: this plan in the blueprint list.
- `.agents/skills/build-app/SKILL.md`: the scaffold's script and the markup convention.
- `AGENTS.md` and `.agents/shared/references/element-references.md`: the agent's guide to a `REF-<id>.json` attachment.

## 16. Deferred and out of scope

- The terminal and the browser: their pages are a ttyd client and a canvas of streamed pixels, and their menus stay as they are.
- The files app: dufs's vendored frontend; a patch there is a re-vendor burden the first pass does not take on.
- An Inspect row that opens developer tools or highlights the element.
- The reverse direction: an agent naming a reference and the shell highlighting the element.
- A chip in the sent bubble (the message names the file as it names any attachment).
- Apps built before this change.
