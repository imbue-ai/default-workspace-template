# The mngr-side changes

The template PR is paired with the mngr repo branch `mngr/better-chat-app-arc`; the two are released together.
Nothing in the mngr repo changes its contract with the workspace: the minds chrome, the vendored embed contract, the forwarder, the share stack, and the service discovery events are untouched.

## Changes

- `apps/minds_evals/imbue/minds_evals/minds_bridge.py`: the bridged calls (`/api/agents/create-chat`, `/api/agents`, `/api/agents/<id>/message`, `/api/agents/<id>/events`) target the chat app's loopback URL instead of port 8000.
  The URL is read from the workspace's registry (`data/.state/apps.toml`, the row named `chat`) through the same bridged exec, with a fallback to `http://127.0.0.1:8010`; the readiness gate polls `GET /_instances` on it.
  The bridge's tests gain the registry read.
- `apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` and the e2e tests that assert on chat markup (`test_creating_page_layout.py`, `test_sync_e2e.py`, `test_snapshot_resume.py`): every chat locator goes through the chat frame inside the workspace frame; the runner already walks frames, so this is a selector change.
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
