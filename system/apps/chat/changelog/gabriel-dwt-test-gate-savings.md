- A plain `uv run pytest` in `system/apps/chat` now skips the `release` tests (the browser tests and the ones that drive a real claude). `-m ''` runs everything, as CI does, and `-m release` runs only the release tests; naming a browser test file without either reports "N deselected" and runs nothing.

- The frontend's `npm test` no longer runs eslint and prettier. `npm run lint` is eslint alone, the new `npm run typecheck` is `tsc --noEmit`, and `npm run format:check` is prettier; `npm run build` still typechecks.
