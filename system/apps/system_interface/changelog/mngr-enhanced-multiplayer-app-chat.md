Every window gets "Chat about this app" (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 9):

- A focused window's title bar is now the window menu, Refresh, and a speech balloon, "Chat about this app", on the left, the app's icon and title centred over the bar, and minimize, maximize, and close on the right as before (`data-window-control="app-chat"`; the menu and Refresh swap places). The balloon is drawn on every window of an app that is not internal, the chat app's included, when some app declares a launch path with `app_params`.

- The balloon runs the first such launch path (the chat app's `app-chat`), naming the window's app and its display name in the params it declares, through the launch route's new `show` target: the page lands where the `show` op would put it (a window already there raised, else the app's pinned window pointed at it for this client, else a new window) and, since the target names the window, the two are snapped side by side for this client, the chat on the half of the backdrop its frame's centre was already on (the right for a pinned window never shown) and the app's window on the other, the chat on top. The launch target `{"kind": "show", "beside"?}` is open to any caller; the shell names no app in any of this.

- `POST /api/desktops/<id>/launch` takes the `show` kind; the `app` object's launch paths carry `app_params`.
