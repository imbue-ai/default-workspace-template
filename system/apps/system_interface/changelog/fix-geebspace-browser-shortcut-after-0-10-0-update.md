Desktop shortcuts of an app that is no longer registered are removed. A workspace updated off the old browser app kept its Browser shortcut on every desktop, and opening it said "browser is not registered". On every read of the desktops the shell now drops the shortcuts of any app the app registry does not list, such as an app a release retired, whose entry the update removed. A registry with no entries drops nothing.

An app can now have several default desktop shortcuts. The Screen gets both a Screen and a Browser shortcut, matching its two launcher entries.

- `default_shortcuts_offered.json` records which `(app, launch)` shortcuts were offered, not which apps (version 2). That way a workspace that already had the Screen shortcut still gets the Browser one. The old list of apps is read as each app's first shortcut and rewritten on the first read.

- The shortcuts that were dropped are cleared from that record, so an app that registers again gets them back. A shortcut the user removed themselves still stays removed.

- The app list in `GET /api/inventory` carries `default_shortcuts` (a list) in place of `default_shortcut`.

- An app's second and later default shortcuts are labeled with their launch path ("Browser"), not the app's name, so the two Screen shortcuts can be told apart.

- Right-clicking any of an app's shortcut entries in the launcher offers to add or remove that shortcut.

- A focus-mode shortcut of a one-window app's POST launch path (the Screen's Browser) always runs it into the app's window, as the launcher's row does, instead of only raising that window, so the Browser shortcut always opens the browser. Other focus shortcuts still just raise the app's last window.
