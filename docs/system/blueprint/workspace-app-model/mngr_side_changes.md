# The mngr-side changes

The template PR is paired with the mngr repo branch `mngr/better-chat-app-arc`; the two are released together.
Nothing in the mngr repo changes its contract with the workspace: the minds chrome, the vendored embed contract, the forwarder, the share stack, and the service discovery events are untouched.
The one mngr-side reader of the workspace's supervisord program lines (the evals evidence collector, below) reads the manifest registration form, since every built-in and every scaffolded program line registers with `--manifest` and no `--name`.

## Changes

- `apps/minds_evals/imbue/minds_evals/minds_bridge.py`: the bridged calls (`/api/agents/create-chat`, `/api/agents`, `/api/agents/<id>/message`, `/api/agents/<id>/events`) target the chat app's loopback URL instead of port 8000. Since the chat-agent split's phase 2, `/api/agents/create-chat` and `/api/agents/<id>/...` are aliases of `/api/chats/create` and `/api/chats/<chat-id>/...` (the id is read as a chat id), kept until phase 7 retargets the bridge; `/api/agents` stays the plain listing of every mngr agent, beside the new `/api/chats`, which lists the chats as snapshots.
  The URL is read from the workspace's registry (`data/.state/apps.toml`, the row named `chat`) through the same bridged exec, with a fallback to `http://127.0.0.1:8010`; the auth readiness gate polls the chat app's `/api/claude-auth/status`.
  The bridge's tests cover the registry read.
- `apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` and the e2e tests that assert on chat markup (`test_creating_page_layout.py`, `test_sync_e2e.py`, `test_snapshot_resume.py`): every chat locator goes through the chat frame inside the workspace frame; the runner walks frames, so this is a selector change. `_send_message_and_await_reply` resolves the chat frame (`_chat_frame`, the workspace frame's child whose URL path is the chat's agent id) and drives the composer and transcript there.
- `apps/minds_evals/imbue/minds_evals/testing.py`: `chat` is in `SELF_REGISTERED_APPS`, since the chat program registers itself from inside its process rather than from a `forward_port.py` call in its supervisord line.
- `apps/minds_evals/imbue/minds_evals/evidence_collection.py`: `parse_supervised_registrations` joins a registry row to the supervisord program whose block registers it, by matching either `forward_port.py ... --name <name>` or the manifest form.
  Every built-in and every build-app-scaffolded program line registers with `forward_port.py --manifest system/apps/<package>/app.toml --url ...` and no `--name`, so a `--manifest` call registers the enclosing `[program:<name>]` (the manifest's `name` is the program's for every such line), exactly as the template's `.agents/skills/migrate-workspace/scripts/migrate_workspace.py` reads it.
  Without this, `resolve_preexisting_registrations` loses the config half that covers a template app which had not registered before the pre-turn snapshot, and `service_entries` resolves every app through its by-name fallback alone.
  `evidence_collection_test.py` covers the manifest-form case.
- `libs/mngr_forward/README.md` and `apps/minds/docs/overview.md`: one sentence each noting that chat is a registered app at its own origin.
- `uncertainties.md`: the default-workspace-template issue #521 entry is resolved by updating the issue once the template PR merges.
- The agent memory note `minds-workspace-os-model.md`: append the settled decisions (tool environments, instance-only tabs, location relay, the terminal mechanism kept).
- `~/handoff/app-cleanup.md`: replaced by a handoff naming the spec folder and the state of both branches.
- Changelog entries: `apps/minds_evals/changelog/mngr-better-chat-app-arc.md`, `apps/minds/changelog/mngr-better-chat-app-arc.md`, `libs/mngr_forward/changelog/mngr-better-chat-app-arc.md`, and `dev/changelog/mngr-better-chat-app-arc.md`.

## Deferred

- The minds chrome forwarding a deep link's query string (`?view=`, `&open=`, `&action=`) to the shell frame it embeds: the shell honours those on load, but nothing in the chrome passes them through. It lands with the switcher, which the meta spec defers.

## Tests

- `minds_bridge_test.py` for the registry read and the fallback.
- The minds e2e suites run in CI against a workspace built from the paired template branch.

## Release order

The template PR merges first and is tagged; the mngr PR merges with the vendored template pin advanced to that tag, so the evals bridge and the e2e suites always see a workspace whose chat app exists.
