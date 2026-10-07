New library and CLI, `workspace-themes`, for workspace themes (`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`).

- Reads theme folders from `system/themes/` (built in) and `themes/` (made in the workspace), resolves each theme's base chain, chrome, and icons, and validates every theme against contract 1: only contract tokens and parts, metrics within range, no classes, ids, imports, or remote URLs (including through a namespace prefix, any selector function, a backslash, or a URL put in with `var()`), and only allowed file types and sizes. `validate <id>` reports every folder with that id.

- Serves a theme's bundle and files from any app's own origin (`register_workspace_theme_route`), with `X-Content-Type-Options: nosniff` on every answer and a script-free `Content-Security-Policy` on an svg (`theme_file_security_headers`). A theme folder, an icon, or an icons folder that is a link is never read or served, and a style file may not be named `theme.css`, the bundle's name.

- Icon tools that work with any image model or none: `icon spec` (what to draw, and which apps lack an icon), `icon fit` (trim, scale, and quantize any drawing onto the theme's limits), `icon grid` (draw from a text pixel grid), `icon check` (an svg icon may hold no script, event handler, `foreignObject`, or reference outside itself), and `icon install` (which, for a built-in theme, fills only an icon the theme does not ship); `fit` and `grid` take `--preview` for an enlarged copy.

- `new` lays out a theme folder, `validate` checks themes, `bundle` prints a bundle, `list` lists themes, and `lint-app` finds literal colors, fonts, and shadows in a tokens-mode app.

- `scripts/vendor-css-library.mjs` adapts a CSS library (system.css, win95.css, ...) onto the contract's parts from a pinned, digest-checked source; it refuses two library assets with one file name, and never reads text inside an attribute selector or string as a class.
