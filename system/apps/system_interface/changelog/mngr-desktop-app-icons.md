- Every app you have gets an icon on the desktop, not only the built-in ones. An app that declares no `default_shortcut` in its manifest, including every app built with `build-app` and every app registered before manifests existed, now gets a shortcut of its first launch path, in focus mode. It is added once to every desktop, including desktops made before this change, and a shortcut you remove stays removed. Previews and isolated test servers, which register without a program, get none.

- Right-clicking an app in the launcher now offers `Add to desktop`, which puts that launch path's shortcut on the desktop on screen, or `Remove from desktop` when it is already there. A taskbar entry's right-click menu offers the same for its window's app.

- Adding or removing a desktop shortcut (from these menus, or Remove on the icon itself) shows on the desktop at once rather than after the shell answers, and is put back if the shell refuses.
