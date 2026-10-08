Workspace themes: Desktop settings has a **Theme** choice listing every theme the workspace has -- Standard, **Classic Mac**, **Windows 2000**, and any theme made in the workspace -- with a live preview (Save keeps it, Cancel puts the saved one back). A preview is never remembered, so a reload mid-preview comes back in the saved theme.

- A theme can be the workspace's default ("Make this the workspace default") or one desktop's own; a desktop with no choice of its own wears the default.

- Window title bars are drawn from the theme's chrome: which controls sit left and right, and whether the title is centred (Classic Mac puts the close box first and centres the title).

- Under a theme, each app's icon is the theme's drawn icon, one the workspace generated, or one derived from the app's standard icon in the theme's style (pixelated, reduced to the theme's colors, or one color), so a new app is never left with a blank or out-of-place icon.

- Editing a theme's files, or an app manifest that changes which themes are available, updates every open window at once, with no restart.

- New routes: `GET /api/themes`, `POST /api/themes/default`, `GET /api/themes/<id>/icons/<file>`, `POST /api/desktops/<id>/theme` (any available theme id, or null for the default), and `/_static/themes/` for theme files. `desktop_themes.json` holds `{"version": 1, "default", "desktops"}`. The socket sends `themes_changed`. An unavailable theme lists no icons, and every icon is answered with `X-Content-Type-Options: nosniff` (an svg icon also with a script-free `Content-Security-Policy`).

- `/theme-gallery?theme=<id>` shows every part of the interface under a theme, for judging one; a theme that fails validation is shown in the standard look with its problems listed.

- The shell's build also produces the page kit (`/_static/workspace_theme.css` and `/_static/workspace_theme.js`), which a plain-HTML app page loads to wear the theme.

- A window's title-bar controls take every press on them, even where a resize corner reaches into the title bar; the corner still resizes from its rim, just outside the window.
