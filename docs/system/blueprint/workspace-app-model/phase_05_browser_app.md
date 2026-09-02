# Phase 5: the browser app

Contracts: [contracts.md](contracts.md) sections 4.3 (browser row) and 5.

## Goal

Give the browser daemon the instances API as an adapter over its fleet, with status from ownership and nudges on fleet changes.

## Files

Created, under `system/apps/browser/src/browser/`:

- `instances.py`: `FleetInstanceSource` (an `InstanceSourceInterface`) mapping `list_instances` to the fleet's browser list (key = name, url `/?session=<name>`, title `Browser <N>` or the legacy name, status per contracts 4.3, lifetime `explicit`, renameable false), `create_instance` for action `new` to the existing create path, `delete_instance` to the existing close path, `set_location` to a navigation of the live browser's active tab followed by a manifest write.
- `instances_test.py`.

Modified:

- `runner.py`: mounts the blueprint on the existing Flask app (the daemon serves its own origin, so `instances_url` is the app URL); the existing `/browsers` routes stay for the CLI.
- `fleet.py` and `session.py`: every fleet event that changes the list or an ownership state (create, close, acquire, release, handoff, crash) calls the nudger.
- `manifest.py`: unchanged shape; the source reads it for pending-restore entries so a restoring browser lists as `error` until it is up.
- `pyproject.toml`: depends on `app-instances`.
- `README.md`.

## Behaviour

- Status is `working` while any agent holds control, `idle` when free or held by the human, `error` for a crashed browser.
- The init gate answers `503` from `GET /_instances` until the restore finishes, so the shell keeps its last list.
- Location for the browser accepts a full URL as well as a path, since navigating a browser to a page is the natural agent verb here.

## Tests

- Source tests over the fleet's test doubles: list mapping, status per ownership state, create and delete delegation, location navigates and records, nudge on each fleet event.
- The existing `browser_test.py`, `fleet_test.py`, and `test_browser_integration.py` keep passing.

## Manual verification

`agentic-browser-fleet new`, then `curl http://127.0.0.1:8081/_instances` shows the browser `working` while the agent holds it and `idle` after `release`.

## Changelog entries

`system/apps/browser/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

The daemon serves the instances routes with correct status and the fleet CLI is unaffected.
