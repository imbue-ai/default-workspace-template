- The frontend's `npm test` no longer runs eslint and prettier. `npm run lint` is eslint alone, the new `npm run typecheck` is `tsc --noEmit`, and `npm run format:check` is prettier; `npm run build` still typechecks.

- The pytest configuration registers the `browser`, `frontend` and `real_claude` markers in place of `release`, like every other suite.
