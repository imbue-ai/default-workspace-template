Desktop settings has a new **Theme** choice: Standard, **Classic Mac** (built on system.css), or **Windows 2000** (built on win95.css, in the Windows 2000 face gray). Picking a theme previews it on the desktop straight away; Save keeps it, and Cancel or closing the dialog puts the saved theme back. Each desktop keeps its own theme, and a saved switch restyles the desktop at once for everyone looking at it.

- Classic Mac: black-and-white windows with pinstriped title bars on the focused window, rounded System 6 buttons, a one-pixel gray dither desktop, Chicago type, and Finder-style icon labels.

- Windows 2000: beveled windows with the navy caption gradient (gray for a window in the background), raised buttons, a sunken launcher field, a teal desktop, a taskbar with the current window's button pushed in, and Tahoma type.

- Under either theme every app's icon becomes a pixel-art one drawn for that theme; an app without one gets a generic program icon. A wallpaper a desktop chose itself still shows.

- The theme is stored in a new `desktop_themes.json` beside `desktops.json` (which keeps its released shape, so a rolled-back shell still reads it), set with `POST /api/desktops/<id>/theme`, and carried on the desktop record as `theme`.
