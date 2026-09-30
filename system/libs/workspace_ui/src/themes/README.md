# Retro themes

The Classic Mac and Windows 2000 looks, worn when the Imbue Studio chrome sends the matching `minds:ui-theme` message (see `uiTheme.ts` and `embed.ts`). Each stylesheet is scoped to its own `:root[data-ui-theme]` value, so a page that never hears from the chrome keeps its default look.

- `mac-classic.css`, `windows-2000.css`: token overrides (colors, fonts, radii, shadows, the desk metrics) and the desktop chrome, found by the shell's stable class names (`.window`, `.title-bar`, `.window-control`, `.taskbar-entry`, `.launcher-field`, `.shortcut-label`).

- `vendor/`: the element-level rules of system.css and 98.css (recolored to Windows 2000), scoped to their theme, with their fonts and licenses.

- `icons/<theme>/<app>.png`: pixel-art app icons; `app.png` is the icon for an app without one of its own.

`vendor/` and `icons/` are copies of Imbue Studio's (`apps/minds/frontend/src/themes/` in mngr), where `scripts/vendor-retro-themes.mjs` and `scripts/generate-retro-icons.mjs` produce them; update both repos together.
