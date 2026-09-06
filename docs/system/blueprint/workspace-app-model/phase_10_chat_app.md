# Phase 10: the chat app

Contracts: [contracts.md](contracts.md) sections 2, 4.3 (chat row), 8, 12, 14, and 15.

## Goal

Move the chat package, process, and frontend out of the system interface into `system/apps/chat`, running from its own tool environment and program at its own port, with the sign-in flow inside the chat, and land the shell's mngr-free invariant as ratchets.

## What landed

Four commits on the arc branch, in this order.

### The package and program

- `system/apps/chat/`: the `chat` package (`imbue/chat/`, console script `chat-app`), moved verbatim from `imbue/system_interface/` with `chat_document.py` as `server.py`, `chat_instances.py` as `instances.py`, `chat_errors.py` as `errors.py`, and `SystemInterfaceState` as `ChatState` (`state.py`); `main.py` registers the manifest and port 8010 through `forward_port.py` at startup (`--no-register` for a throwaway boot) and starts `mngr observe`; `config.py` reads `CHAT_HOST`, `CHAT_PORT`, `CHAT_JAVASCRIPT_PLUGINS`, `CHAT_STATIC_PATHS`.
- The chat serves its own WebSocket at `/api/ws` (`agents_updated` and the proto-agent messages), `/api/health`, the chat document with the terminal app's origin label from the registry in a meta tag, and everything it served before.
- `system/supervisord.conf` runs `[program:chat]` in the `chat` band; the shell's line registers only its own manifest.
- `system/config/mngr_plugins.toml` assigns the five harness plugins to `chat` beside `mngr`; the shell's tool has none.
- The shell keeps `main.py` over a slim `SystemInterfaceState`, `config.py` (host and port), `documents.py`, `request_helpers.py`, and the `shell/` subpackage; `wsgi_dispatch.py` and the path dispatch are gone.
- `test_project_ratchets.py` in the shell walks every non-test module's imports for `imbue.mngr*` and `imbue.chat` (an AST scan: import-linter's scanner panics on this package once external packages are included), keeps the "never runs `mngr`" regex, and forbids the literal `"chat"` in the shell package and its frontend.
- Tests moved with the code; `imbue.chat.testing.running_workspace` is the two-process fixture (the chat app and the shell on two ports over a registry) the chat's e2e tests and the shell's `test_chat_system.py` share.
- The update apply's health probe polls the shell's `/api/health` and then the chat's (its URL from the registry, `http://127.0.0.1:8010` without a row); the apply plan refreshes the `chat` tool like every app's.
- `system/scripts/default_account_args.py` and `migrate_claude_auth.py` import `imbue.chat` from the root venv rather than shimming to the tool's entry points.

### The sign-in flow

- `new` launches at once on the most recently used account.
  With nothing signed in it mints a chat that waits for an account (`ProvisionalChatPhase.AWAITING_ACCOUNT`); its page shows the provider chooser, and a sign-in launches the chat under the same id through `POST /api/agents/create-chat` with `agent_id`.
- A provisional chat has a phase (`awaiting_account`, `creating`, `failed`), and the instances API maps it to `attention`, `working`, and `error`.
  A failed create keeps the record with the reason (the exit status and the last lines `mngr create` printed) and the page offers a retry on the same account; deleting a waiting or failed chat drops it.
- The streamed creation log and its socket are gone; while the create runs the page shows the composer over an empty transcript, and a message typed then is held as "Sending" until the agent registers (`whenAgentRegistered`), then sent.
- The shell lost its provider chooser, the first-run greeting, the launcher's provider picker, and every chat special case; creating a project only switches to it and lands on the New Tab page.
- The chat page creates subagent instances through its own `/_instances` and reads the terminal app's origin label from its document.
- `layout.py` posts `{op, args, requester}` with the caller's own chat as an address; the shell resolves `self` and attributes the op from that address, and the `layout_op` message carries `requester`.

### The frontend split

- One npm workspace rooted at `system/package.json` (one `npm ci`, one lockfile, shared `eslint.config.js`, `.prettierrc`, `tsconfig.base.json`) with three members: `system/libs/workspace_ui` (source only), `system/apps/system_interface/frontend`, and `system/apps/chat/frontend`.
- The library holds the design system's token layer (`src/base.css`, imported by each app's stylesheet after `@import "tailwindcss"`), the shared components, `DestroyConfirmDialog`, `portal`, `flyout-position`, the base helpers (`base-path`, `origin`, `addresses`, `views`, `models/ClientIdentity`, `http`, `backoff`, `ws-json`, `request-error`), and the boundary modules (`app_contract`, `embed` with the vendored contract alias, `terminalFocus`); the apps import it as `@imbue/workspace-ui/src/<module>`.
- The chat frontend is the old `src/chat/` plus the provider UI (`Providers`, `ProviderChooserModal`, `accountRow`, `providerSignInStyles`, `providerMarks`, `removeAccountDialog`, `modelCardStyles`) and the chat half of the stylesheet, building into `imbue/chat/static/`; the shell builds `index.html` and the contract module (from the library's source) into its own `static/`.
- Each build stamps its bundle with the tree hashes of its frontend directory, the library, and the lockfile; the update apply builds at the npm root, snapshots and verifies both bundles, refreshes `node_modules` at the npm root, and takes `--worker-bundle <app>=<path>` per app (installed only as a pair).
- The Dockerfile copies the workspace root's manifests and every member's `package.json`; `install_dependencies.sh` runs `npm ci` and `build_workspace.sh` runs `npm run build` at `system/`; CI does the same.

## Decisions taken on the way

- Two separate builds over one shared library rather than one multi-entry build: the chat app owns its bundle and the shell never compiles chat code.
- The shell keeps no chat name anywhere: the op body carries the requester's address, and the ratchet holds it.
- Small shared backend modules (the broadcaster, the documents and request helpers, the WSGI server, the config) are duplicated and trimmed per package rather than lifted into a library.
- The `--worker-bundle` flag is per app and all-or-nothing, because one build emits both bundles.
- The chat's ratchet file uses mngr's `test_ratchets.py` set with `ty`; `test_meta_ratchets.py` exempts the chat package like the shell.
- The evals bridge's two tests of the created page and the recovery e2e tests keep the pre-phase-10 shape (a chat page opened by its own URL) because they model the window before the shell lists the chat.

## Tests

- Every moved test passes under the new package name; the shell suite runs without mngr installed in its tool.
- `test_project_ratchets.py` in the shell: the import scan and both regex ratchets are at zero.
- The shell's e2e suite runs over two stub apps; the chat's e2e suite boots the two-process fixture and, in this phase, covers the sign-in flow (a chat without an account offers the chooser in its own tab, a chat with one starts at once and shows its composer when it lands).
- `test_chat_system.py` boots both processes and asserts the shell's inventory carries the chat app's instances with status, and that a rename through the relay relists.
- The update apply's suite covers the second bundle, the npm root, and the per-app worker bundles.

## Manual verification

Chats create, rename, delete, stop, and show status; a permission card reaches the minds inbox through the relay; a fresh workspace lands on New Tab and its first chat greets with `/welcome`; `supervisorctl stop chat` shows stopped placeholders and `start` restores them.

## Changelog entries

`system/apps/chat/changelog/mngr-better-chat-app-arc.md` (new project), `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

`uv tool list` shows `chat` with the harness plugins and `system-interface` with none; the ratchets pass; every manual check above holds.
