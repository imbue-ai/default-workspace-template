The browser app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The browser's instances API, its shell nudge, and the fleet's instances adapter are deleted; a browser session is a window at `/?session=<name>`, and the fleet pulls a session into the desktop through `layout.py open browser --path "/?session=<name>"`.

- Session records stay in `data/.apps/browser/instances.json`; a start URL must be an absolute `http(s)` URL (`InvalidStartUrlError` otherwise).

- The manifest declares launch paths only; the retired `instances`, `instances_url`, and `actions` keys are gone.
