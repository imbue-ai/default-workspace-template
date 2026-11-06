- `workspace-themes lint-app` checks an app's own `assets/` folder too, where build-app keeps a page's hand-written CSS and scripts; built output (`static/`, `dist/`, `build/`) is still skipped.

- `workspace-themes lint-app` lints outside-mode apps too, and takes a token's `var(--token, fallback)` fallback as the look the app keeps with no theme, not as a literal. An id selector that starts like a hex color (`#add-form`) is no longer read as one.
