Phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`), the parts outside any one app:

- `system/scripts/layout.py` is rewritten around the desktop's verbs (`context`, `desktops`, `list`, `load`, `open`, `focus`, `minimize`, `restore`, `maximize`, `place`, `close`, `navigate`, `refresh`, `shortcuts`, `shortcut set|move|remove`, `wallpaper`); it names apps and windows (`win-<hex>`, `self`, or an app name), opens at `--path` or a launch path (`--launch`, `--param`), targets one client (`--client`, else the client that last messaged the requester, else the one connected client), and refuses the tabbed shell's `app:`/`chat:`/`terminal:` addresses and its retired verbs with a pointer to the replacement. The read verbs print the shell's inventory document.

- The `app_instances` library (instance records, the JSON store, the blueprint, the nudge, the sidecar) is deleted, as is `system/scripts/migrate_workspace_layouts.py`.

- The shared contract module (`system/libs/workspace_ui/src/app_contract.ts`) loses `open(address)` and the handshake's `deviceKind`, `address`, `tabId`, and `viewId`; the handshake is `{clientId, windowId, desktopId, path}`. `ClientIdentity` keeps the active desktop id beside the client id (`getActiveDesktopId`, `adoptClientIdentity({clientId, desktopId})`). `addresses.ts` and `views.ts` are deleted, and the stale dockview comments in the library describe the desktop.

- The files app's supervisord program runs dufs directly after registering the app; the root `pyproject.toml` no longer lists the deleted packages, and `uv.lock` follows.

- The auto-open in `.mngr/settings.toml` posts the desktop's `open` of the chat at `/?chat=<id>`.

- Docs: `docs/system/README.md` points at the desktop plan as the current direction; the plan records that every phase has landed; `contracts.md` describes the contract module without `open(address)`; `system/apps/README.md` and `system/libs/README.md` describe the desktop-era layout.
