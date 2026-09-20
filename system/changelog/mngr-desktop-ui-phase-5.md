Phase 5 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`), the parts outside any one app:

- The shared browser-side app contract (`system/libs/workspace_ui/src/app_contract.ts`): the shell's handshake now carries `windowId`, `desktopId`, and `path` (the window the page is in, its desktop, and the path the window is at), read as `""` from a shell that does not send them; the tabbed shell's `deviceKind`, `address`, and `tabId` stay until phase 6 deletes them, and `viewId` still carries the desktop id for the chat's client-activity report.

- `contracts.md` gains the `--desk-shortcut-label-shadow` theme token, the text shadow the backdrop's shortcut labels wear over a wallpaper.

- The shared `postJson` helper (`system/libs/workspace_ui/src/models/http.ts`) throws an `HttpError` carrying the response status alongside the server's detail, so a caller can tell one refusal from another (the desktop's placements save reads a 409 as a stale save through it).

- `dockview-core` leaves the npm workspace lockfile along with the tabbed frontend that used it.

- The library's address and view helpers (`system/libs/workspace_ui/src/addresses.ts`: `addressFor`, `parseAddress`; `src/views.ts`: `EVERYTHING_VIEW_ID`, `isEverythingView`) are removed: with the chat opening pages by path and the tabbed shell's models gone, nothing imported them.
