Removed the `just minds-css` recipe and the Tailwind `build:css` steps from the repo tooling: the minds desktop client's legacy JinjaX front end (and its `app.css` stylesheet) was deleted in favor of the Mithril SPA, so `minds-test-electron` / `minds-test-electron-flow` no longer depend on a CSS compile, and `scripts/snapshot_minds_e2e_state.py` no longer runs `pnpm run build:css` when baking the e2e image.

`uv.lock` and `apps/minds/pnpm-lock.yaml` were regenerated for the dropped `jinja2`/`jinjax` Python dependencies and the dropped `tailwindcss`/`@tailwindcss/cli`/`concurrently` npm dev dependencies. The `minds-justfile` and `minds-dev-workflow` skill docs were updated to match, and the root `.gitignore` no longer carries the ignore rule for the deleted `app.min.css` build artifact.

`mirror/overlay/uv.lock` (the public-mirror lock overlay) was regenerated for the same dependency removals, matching what `mirror/materialize_public_tree.sh --lock` produces.
