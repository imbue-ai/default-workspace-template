The shared UI library can wear Imbue Studio's retro themes.

- `embed.ts` handles the embed contract's new `minds:ui-theme` message (contract version 7): the theme Imbue Studio's chrome wears, sent after the workspace announces it is ready and on every switch.

- `themes/`: the Classic Mac and Windows 2000 stylesheets (token overrides plus the element rules of system.css and a Windows 2000 recolor of 98.css, all scoped to `:root[data-ui-theme]`), their pixel-art app icons, and `uiTheme.ts`, which applies a theme and finds an app's icon. The vendored CSS and icons are copies of Imbue Studio's (`apps/minds/frontend/src/themes/` in mngr).
