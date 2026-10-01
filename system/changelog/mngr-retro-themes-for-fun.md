The shared UI library carries the retro themes a desktop can wear.

- `src/themes/`: the Classic Mac and Windows 2000 stylesheets, each built on its library (system.css; win95.css recolored to the Windows 2000 face gray), scoped to `:root[data-ui-theme]`, with the libraries' fonts, images and licenses, their pixel-art app icons, and `uiTheme.ts`, which applies a theme and finds an app's icon.

- The shared button, dialog and menu carry the libraries' class names (`btn`, `modal-content`/`standard-dialog`, `dropdown-menu`/`dropdown-item`), which style them only while a retro theme is worn.

- `scripts/vendor-retro-themes.mjs` regenerates the scoped library copies from pinned, digest-checked sources, and `scripts/generate-retro-icons.mjs` draws app icons with Retro Diffusion (it needs `RETRODIFFUSION_API_KEY`).
