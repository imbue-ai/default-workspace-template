# Phase 5: the browser app

Contracts: [contracts.md](contracts.md) sections 4.2, 4.3 (browser row), 5, and 17.
User-visible behaviour is unchanged: the browser tab, the fleet CLI (`agentic-browser-fleet`), and the shell's `/api/browsers` passthroughs behave exactly as before; the shell reads the new routes only from phase 7.

## Goal

Give the browser daemon the instances API as an adapter over its fleet, with status from ownership and nudges on fleet changes.

## Files

Created, under `system/apps/browser/src/browser/`:

- `instances.py`: `FleetInstanceSource` (an `InstanceSourceInterface`) over a `FleetInterface`: `list_instances` maps every browser to a record (key = name, url `/?session=<name>`, title `Browser <N>` or the legacy name, status per contracts 4.3, lifetime `explicit`, `last_active` `null` since the fleet clocks agent activity on a monotonic lease timer, renameable false); `create_instance` for action `new` (no params) to the fleet's create; `delete_instance` to the close that also drops the manifest entry and deletes the profile (a key no browser can have is a no-op); `rename_instance` refuses; `set_location` takes an absolute `http(s)` URL (a rooted path is a `400`), navigates the live browser's active tab, and returns the record.
  The pure mapping is `instance_record_for_browser` and `instance_status_for_browser`.
- `interfaces.py`: `FleetInterface`, the narrow view of the fleet the adapter needs (`is_ready`, `list_browsers`, `create_browser`, `close_browser`, `navigate_browser`), so the adapter is tested over an in-memory fake without the async loop.
- `bridged_fleet.py`: `BridgedFleet`, the real fleet reached the way every daemon route reaches it (one coroutine per verb, run on the loop through the bridge), and `ManagerNudger`, the blueprint's nudger, which forwards to whatever nudger the manager has installed.
- `data_types.py` (`BrowserLifecycle`, `BrowserController`, `BrowserSnapshot`), `primitives.py` (`APP_NAME`, the registered name the manifest declares, which names the shell nudge route and the instance store; `BrowserName`, the browser's name validated as its instance key; `derive_browser_title`; `instance_url_for_browser`), `errors.py` (the fleet's refusals: `FleetCreateRefusedError`, `UnknownBrowserError`, `BrowserNotDrivableError`, `BrowserHeldByAgentError`, `NavigationFailedError`).
- Tests: `instances_test.py` and `primitives_test.py` over `mock_fleet_test.py`'s `FakeFleet`; `test_browser_instances.py` drives the blueprint on the daemon's real Flask app over the real manager and bridge with fake in-memory browsers; `browser_test.py` gained the nudge cases over the real state machine.

Modified:

- `runner.py`: mounts `build_instances_blueprint(FleetInstanceSource(fleet), ManagerNudger(manager))` at import time in `_register_routes`, over the module-level manager and bridge like every other route (the daemon serves its own origin, so `instances_url` is the app URL); `main` installs the real nudger, `ThreadedNudger(ShellNudger(...))`, on the manager before serving, the way it starts the OOM sweep there, so tests that build the app post nothing to the shell; the app's registered name comes from `browser.primitives.APP_NAME`; `close_browser` calls the manager's `close_and_forget`.
- `session.py`: the manager and every `LiveBrowser` carry a nudger (`BrowserSessionManager.set_nudger`; a `SilentNudger` by default, so a browser built on its own reports to nobody) and call it on every event that changes the list or a status: registration, a launch reaching `running`, a launch failure, a close, a crash, and every ownership write (`_write_control_locked`, the one writer, which acquire, release, handoff, take-control, return-to-agents, and the sweeps all go through). New coroutines for the adapter: `snapshot_browsers`, `create_snapshot`, `close_and_forget` (the close sequence the runner's route spelled out, with the profile delete off the loop), `navigate_browser`, and `LiveBrowser.navigate_active_tab` (refused while an agent holds the browser and while it is not `running`; bounded like the restore navigation; checkpoints the manifest after). The dead `_ALLOWED_NAV_SCHEMES` constant is gone.
- `cdp_client.py`: `navigate(target_id, url)`, a flattened attach, `Page.navigate`, and a detach; Chromium's `errorText` (an unresolvable host, a refused scheme) is raised as a `CdpError`.
- `manifest.py`: the manifest lives at `data/.apps/browser/instances.json` (`app_store_path`, per contracts section 17); reads fall back to the old `data/.state/browser-fleet.json` until the first write to the new path (`# CLEANUP:` after the release carrying this phase). Its shape is unchanged.
- `pyproject.toml`: depends on `app-instances` and `app-manifest`; `uv.lock` re-locked.
- `README.md` (an Instances section and the manifest's new home), `system/apps/README.md`, `data/.state/README.md`, the browser's and the root changelog entries.
- `system/libs/app_instances`: `AbsoluteHttpUrl` and `LocationTarget` (`LocationPath | AbsoluteHttpUrl`, what `LocationRequest.path` and `set_location` now take; the JSON store answers a URL with a `400`), `ThreadedNudger`, `SilentNudger`; see its changelog.
- `contracts.md`: section 4.2's `path` rule admits the absolute URL form; section 4.3's browser row lists a launching browser as `idle`, names the `409` cases of a location, and says a rooted path is `400` for the browser.

## Behaviour

- Status is `working` while an agent holds control, `idle` when free, held by the human, or still launching (a launch in progress is not an error, and the shell has no starting state), `error` once the browser crashed.
- `GET /_instances`, delete, and location answer `503` until the daemon's init gate opens (after the restore, or at once when Chromium is not installed yet), as the daemon's own state-changing routes do; create does not wait, because `POST /browsers` takes a create during restore and the shell's next fetch picks the browser up.
- Create with the fleet full, or with Chromium not installed, is a `409` carrying the daemon's own reason.
- A location report must be an absolute `http(s)` URL; it navigates the active tab in place (the first page when none was foregrounded yet) and checkpoints the manifest, so a restart restores the new page. It is a `409` while an agent holds the browser (agents are never preempted, and the shell's relay is no exception), while the browser is launching or crashed, and when Chromium refuses the navigation. The instance `url` stays `/?session=<name>`.
- Nudges leave the event loop at once: the manager's nudger is a `ThreadedNudger`, so a slow or absent shell never stalls a browser. Until phase 7 the shell has no `changed` route and every nudge is a debug-level log.

## Tests

- Source tests over the fake fleet: the record mapping for numbered and legacy names, status per ownership and lifecycle, create delegation and its refusals, delete delegation (unknown and impossible keys included), rename refused, location navigating and returning the record, the `400` for a path, the `404` and `409` cases, and the init gate on reads, delete, and location but not create.
- Blueprint tests on the daemon's Flask app: `503` before the gate, the list following acquire, release, and crash of a fake running browser, rename and a rooted location refused, a location `409` while an agent holds the browser, delete nudging and dropping the browser, create answering `409` with the install reason while Chromium is absent.
- State-machine tests: one nudge per ownership write (none for a same-agent re-acquire), one per crash, one per registration and close, and `set_nudger` reaching browsers registered before it.
- The existing `browser_test.py`, `fleet_test.py`, and `test_browser_integration.py` keep passing.

## Manual verification

Done with the daemon on its real port against a stand-in shell that logged every request: without Chromium, every instances route answered as the contract says (the empty list, `409` with the install reason on create, `204` on an unknown delete, `400` on a bad key and on rename, `404` on a location for an unknown browser) and the shell saw the nudge.
The new CDP navigation was exercised against a real headless Chromium over its own debug port: the tab landed on a local page, and an unresolvable host came back as the `Page.navigate` error.
A full daemon run with a real browser (`agentic-browser-fleet new`, then `curl http://127.0.0.1:8081/_instances` showing `working` while the agent holds it and `idle` after `release`) needs Fortress, which the development machine does not have and whose profile Playwright's Chromium could not create there; it is left for the live container.

## Deferred

- The shared process-test harness the files app copied from the terminal stays where it is: the browser is not a sidecar and adds no third copy.
- `last_active` is `null`; the fleet keeps no wall-clock activity time.

## Changelog entries

`system/apps/browser/changelog/mngr-better-chat-app-arc.md`, `system/changelog/mngr-better-chat-app-arc.md`, and `system/libs/app_instances/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

The daemon serves the instances routes with correct status and the fleet CLI is unaffected.
