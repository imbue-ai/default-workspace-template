# The mngr-side changes

The template PR is paired with the mngr repo branch `mngr/better-chat-app-arc`; the two are released together.
Nothing in the mngr repo changes its contract with the workspace: the minds chrome, the vendored embed contract, the forwarder, the share stack, and the service discovery events are untouched.
The one mngr-side reader of the workspace's supervisord program lines (the evals evidence collector, below) must learn the manifest registration form, since phase 1 removes `--name` from every built-in and every scaffolded program line.

## Changes

- `apps/minds_evals/imbue/minds_evals/minds_bridge.py`: the bridged calls (`/api/agents/create-chat`, `/api/agents`, `/api/agents/<id>/message`, `/api/agents/<id>/events`) target the chat app's loopback URL instead of port 8000.
  The URL is read from the workspace's registry (`data/.state/apps.toml`, the row named `chat`) through the same bridged exec, with a fallback to `http://127.0.0.1:8010`; the readiness gate polls `GET /_instances` on it.
  The bridge's tests gain the registry read.
- `apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` and the e2e tests that assert on chat markup (`test_creating_page_layout.py`, `test_sync_e2e.py`, `test_snapshot_resume.py`): every chat locator goes through the chat frame inside the workspace frame; the runner already walks frames, so this is a selector change. Landed with phase 6: `_send_message_and_await_reply` resolves the chat frame (`_chat_frame`, the workspace frame's child whose URL path is the chat's agent id) and drives the composer and transcript there.
- `apps/minds_evals/imbue/minds_evals/testing.py`: `chat` joins `SELF_REGISTERED_APPS` with phase 6. The shell's supervisord line registers the chat manifest beside its own, and the config join reads a `--manifest` call as the enclosing program's registration, so the config half sees the shell once and the registry half is what sees the chat row.
- The bridge item above is phase 10's: in phase 6 the chat row's URL is the shell's own port and every chat route is dispatched by path, so `http://127.0.0.1:8000/api/agents/...` keeps working unchanged.
- `apps/minds_evals/imbue/minds_evals/evidence_collection.py`: `parse_supervised_registrations` joins a registry row to the supervisord program whose block registers it by matching `forward_port.py ... --name <name>`.
  Every built-in and every build-app-scaffolded program line now registers with `forward_port.py --manifest system/apps/<package>/app.toml --url ...` and no `--name`, so the join must also read the manifest form: a `--manifest` call registers the enclosing `[program:<name>]` (the manifest's `name` is the program's for every such line), exactly as the template's `.agents/skills/migrate-workspace/scripts/migrate_workspace.py` reads it.
  Without this, `resolve_preexisting_registrations` loses the config half that covers a template app which had not registered before the pre-turn snapshot, and `service_entries` resolves every app through its by-name fallback alone.
  `evidence_collection_test.py` gains the manifest-form case.
- `libs/mngr_forward/README.md` and `apps/minds/docs/overview.md`: one sentence each noting that chat is a registered app at its own origin, like the terminal.
- `uncertainties.md`: the default-workspace-template issue #521 entry is resolved by updating the issue once the template PR merges.
- The agent memory note `minds-workspace-os-model.md`: append the settled decisions (tool environments, instance-only tabs, location relay, the terminal mechanism kept).
- `~/handoff/app-cleanup.md`: replaced by a handoff naming the spec folder and the state of both branches.
- Changelog entries: `apps/minds_evals/changelog/mngr-better-chat-app-arc.md`, `apps/minds/changelog/mngr-better-chat-app-arc.md`, `libs/mngr_forward/changelog/mngr-better-chat-app-arc.md`, and the existing `dev/changelog/mngr-better-chat-app-arc.md`.

## Tests

- `minds_bridge_test.py` for the registry read and the fallback.
- The minds e2e suites run in CI against a workspace built from the paired template branch, as they do today.

## Release order

The template PR merges first and is tagged; the mngr PR merges with the vendored template pin advanced to that tag, so the evals bridge and the e2e suites always see a workspace whose chat app exists.
