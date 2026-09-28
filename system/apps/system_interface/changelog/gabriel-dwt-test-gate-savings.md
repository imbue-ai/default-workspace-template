- The shell's tests are marked by what they are: `browser` (the end-to-end file) and `frontend` (the embed checks, and the project, glyph-palette, banner-message and not-built-page coordinate checks that read frontend source); the `release` marker is gone. A plain `uv run pytest` in `system/apps/system_interface` now skips the browser tests. `-m ''` runs everything, as CI does, and `-m browser` runs only the browser tests; naming a browser test file without either reports "N deselected" and runs nothing.

- The layout pipeline tests serve their shell on a free port instead of a fixed one, so two runs of the shell's suite on one machine no longer collide.

- The frontend's `npm test` no longer runs eslint and prettier. `npm run lint` is eslint alone, the new `npm run typecheck` is `tsc --noEmit`, and `npm run format:check` is prettier; `npm run build` still typechecks.
