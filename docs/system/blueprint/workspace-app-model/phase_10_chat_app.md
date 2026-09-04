# Phase 10: the chat app

Contracts: [contracts.md](contracts.md) sections 2, 4.3 (chat row), 14, and 15.

## Goal

Move the chat package and process out of the system interface into `system/apps/chat`, running from its own tool environment and program at its own port, and land the shell's mngr-free invariant as an import contract and a ratchet.

## Files

Created:

- `system/apps/chat/pyproject.toml`: name `chat`, entry points `chat-app`, `chat-default-account-args`, `chat-migrate-claude-auth`; dependencies `imbue-mngr`, the five harness plugins, `app-instances`, `app-manifest`, `oom-priority`, `tk-command-parsing`, `imbue-common`, and the runtime deps the shell drops (`pexpect`, `pyte`, `watchdog`).
- `system/apps/chat/imbue/chat/`: every module moved from `imbue/system_interface/` that is not under `shell/` or the shell's serving files, renamed to `imbue.chat.*`: `agent_discovery`, `agent_manager`, `accounts`, `accounts_endpoints`, `activity_state`, `attachments`, `event_queues`, `file_serving`, `harnesses/`, `latchkey_endpoints`, `naming`, `oom_prioritizer`, `presence`, `chat_instances`, `chat_document` (becomes `server.py`), `watcher_common`, `models` (the chat half), `config` (the chat half), and their tests, plus a new `main.py` (`chat-app`: registers through `forward_port.py --manifest`, builds `ChatState`, starts observe, serves on port 8010 with the same threaded server) and `state.py` (`ChatState`, the former `SystemInterfaceState` minus the shell fields).
- `system/apps/chat/frontend/`: `package.json`, `vite.config.ts`, `index.html`, `src/` from the shell frontend's `src/chat/` (which already holds `hooks.ts`, `slots.ts`, `plugin-routes.ts`, and `llm-api.ts` since phase 6) plus the shared modules it still imports from the shell tree copied in (`base-path.ts`, `origin.ts`, `embed.ts` and the vendored contract alias, `app_contract.ts`, `models/AgentManager.ts`'s agent half, `models/ClientIdentity.ts`, `models/Providers.ts`, the provider chooser and its account rows and styles, `views/AgentTerminalPanel.ts`, `style.css` tokens); builds into `imbue/chat/static/`.
- The provider sign-in chooser, the launcher's provider picker, and the first-run greeting leave the shell in this phase (phase 6 kept them there: a fresh workspace needs a way to sign in before any chat page exists, and the page-side create flow that binds an account after the fact is this phase's). The shell's New Tab page then offers the chat app's `new` action like any other app's, and the chat app's `new` with no signed-in account answers the `409` the shell shows verbatim, with the sign-in surface reachable from the chat app's own page.
- `system/apps/chat/README.md` (the chat half of today's shell README), `changelog/mngr-better-chat-app-arc.md`, `test_chat_ratchets.py`, `test_project_ratchets.py` (the conservation and message-lifecycle suites move here as `test_*.py`).
- `system/apps/system_interface/test_project_ratchets.py`: runs `lint-imports` in-process against the new `[tool.importlinter]` contract forbidding `imbue.system_interface` from importing `imbue.mngr`, `imbue.mngr_*`, `imbue.chat`, and a regex ratchet forbidding `subprocess` invocations of `mngr` under the shell package.

Modified:

- `system/supervisord.conf`: `[program:chat]` runs `chat-app` at band `chat`; `[program:system_interface]` no longer registers the chat row.
- `system/apps/chat/app.toml`: `program = "chat"`; `instances_url` omitted (the app URL, `http://localhost:8010`).
- `system/config/mngr_plugins.toml`: the five harness plugins list `tools = ["mngr", "chat"]`.
- `system/apps/system_interface/pyproject.toml`: drops `imbue-mngr`, every plugin, `pexpect`, `pyte`, `watchdog`, and `tk-command-parsing`; adds `app-manifest`, `app-instances` (for `testing`).
- `system/apps/system_interface/imbue/system_interface/main.py` and `server.py`: the dispatcher goes; the shell serves alone.
- `system/apps/system_interface/frontend/vite.config.ts`: two entries (`index.html`, the contract library).
- `system/scripts/default_account_args.py` and `migrate_claude_auth.py`: one-line shims that `exec` the chat tool's entry points, so `run_automation.sh` and the docs keep their paths.
- `system/scripts/build_workspace.sh`: builds both frontends; the tool loop already installs `chat`.
- `.agents/skills/update-self/scripts/update_apply.py`: `critical` bundles are `imbue/system_interface/static` and `imbue/chat/static`; `--worker-bundle` takes a repeated `<app>=<path>`.
- `.mngr/settings.toml`: comments naming the system interface as the creator of chats name the chat app.

Deleted from the shell package: everything moved above and `wsgi_dispatch.py`, `chat_document.py`.

## Behaviour

- The chat app starts its own `mngr observe`, exactly as the shell did; the shell never touches mngr.
- The provisional-instance flow, subagent instances, presence, the first-chat claim with the `first` template, and `/welcome` are unchanged from phase 6 in behaviour; the chat page's `openSubagentTab` creates the subagent instance through the shell's relay route rather than the chat app's own `/_instances` (phase 6's `# CLEANUP:`).
- Provider accounts stay under `~/.minds/accounts`; `migrate_claude_auth` runs from the chat tool.
- The shell's not-built placeholder still embeds the terminal; a stopped or crashed chat app leaves the shell up with every chat tab showing the stopped placeholder.

## Tests

- Every moved test passes under the new package name.
- `test_project_ratchets.py` in the shell: the import contract holds and the regex ratchet is at zero.
- The shell's e2e suite runs with the stub app only; the chat e2e suite (moved) boots `chat-app` against the fake-agent fixtures as `test_e2e.py` does today.
- A new integration test boots both processes and asserts the shell's inventory carries the chat app's instances with status.

## Manual verification

Chats create, rename, delete, stop, and show status; a permission card reaches the minds inbox through the relay; a fresh workspace lands on New Tab and its first chat greets with `/welcome`; `supervisorctl stop chat` shows stopped placeholders and `start` restores them.

## Changelog entries

`system/apps/chat/changelog/mngr-better-chat-app-arc.md` (new project), `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

`uv tool list` shows `chat` with the harness plugins and `system-interface` with none; the import contract test passes; every manual check above holds.
