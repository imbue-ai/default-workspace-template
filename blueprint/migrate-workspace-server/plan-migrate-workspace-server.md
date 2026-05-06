# Migrate minds workspace server into the template as `apps/system_interface`

## Overview

- Promote the minds workspace server to a first-class app in this template at `apps/system_interface/`, removing it from `mngr` (both `~/utilities/mngr` and the vendored copy at `vendor/mngr`).
- Folder name is renamed to match the existing `system_interface` service entry in `services.toml`; everything else (distribution name, CLI command, Python module path, env vars, class names) stays as-is for this PR. A deeper rename is a follow-up.
- Keep coupling to `mngr` intact: the server keeps importing `imbue.mngr.*`, `imbue.concurrency_group.*`, `imbue.imbue_common.*` and shelling out to `mngr observe`. Those deps continue to resolve through the vendored `vendor/mngr` workspace.
- The two repos are allowed to drift: this template's PR deletes only the vendored `vendor/mngr/apps/minds_workspace_server/` directory; the source-of-truth deletion in `~/utilities/mngr` lands in a separate PR on a fresh worktree.
- Frontend rebuild/refresh wiring is out of scope; built `static/` assets stay committed so a freshly-created mind comes up with a working UI.

## Expected behavior

- After this PR lands and a fresh mind is created from the template, the bootstrap service manager starts the `system_interface` service, which still runs `minds-workspace-server`, listening on `127.0.0.1:8000` with the existing UI served from committed `static/` assets.
- The minds desktop client (in the still-extant `apps/minds` of `mngr`) talks to the workspace server over HTTP at `localhost:8000` exactly as before — no change to its behavior.
- `uv run minds-workspace-server` from the template root continues to work and resolves to the new location via the workspace.
- Running the project's tests includes the new app's tests; failures in `apps/system_interface` block CI.
- Frontend changes still require the developer to manually run `npm install && npm run build` inside `apps/system_interface/frontend/`; no auto-rebuild or auto-refresh exists yet.
- In `~/utilities/mngr`, after the companion PR lands, `apps/minds_workspace_server/` and `specs/minds-workspace-server/` are gone, and there are no broken references in mngr's code, docs, or tests.
- `vendor/mngr` in this template will diverge from `~/utilities/mngr` (template-local deletion of one app directory). Subsequent `git subtree pull` operations need to be done with awareness; no documentation note is added for now.

## Changes

### In this template (forever-claude-template)

- Move (effectively `git mv`) `vendor/mngr/apps/minds_workspace_server/` to `apps/system_interface/`, with all internal contents — `imbue/minds_workspace_server/` package, frontend project, committed `static/` build artifacts, tests, fixtures, ratchets, and `pyproject.toml` — preserved unchanged.
- Update root `pyproject.toml`:
  - Add `apps/system_interface` to `tool.uv.workspace.members`.
  - Add `minds-workspace-server = { workspace = true }` under `[tool.uv.sources]`.
  - Add `minds-workspace-server` to root project dependencies so `uv run minds-workspace-server` resolves from the template root.
- Add a small `apps/README.md` marker explaining what `apps/` is for (parallel to how `libs/` is organized; the first app introducing this directory).
- Move the spec from `~/utilities/mngr/specs/minds-workspace-server/` (currently `concise.md`) into this template at `specs/system_interface/concise.md`.
- Consolidate the moved app's frontend gitignore patterns (e.g. `node_modules/`, `dist/`, build caches) into the template's root `.gitignore` so previously-ignored files stay ignored after the move; remove the now-redundant local `.gitignore` files inside the moved directory.
- Verify `services.toml` requires no edits — the existing `system_interface` service already invokes `minds-workspace-server` and that CLI entry point is unchanged.
- Wire the new app into the template's CI / test runner so `apps/system_interface` tests run alongside the existing `libs/` tests; the moved `pyproject.toml` already configures pytest, so this is about discovery, not test config.
- Manually verify a fresh checkout: `uv sync --all-packages`, then start the `system_interface` service via the bootstrap manager, then load the UI in a browser and confirm the existing endpoints (agent list, SSE, WebSocket) work.

### In `~/utilities/mngr` (companion PR, separate worktree)

- Create a fresh worktree at a path outside the existing checkouts, branch `gabriel/remove-minds-workspace-server`.
- Delete `apps/minds_workspace_server/` in its entirety.
- Delete `specs/minds-workspace-server/`.
- Grep the rest of mngr for any references to the deleted app — READMEs, docs, scripts, e2e test comments — and remove or update them in the same PR.
- Run mngr's full test suite to confirm no remaining references break anything.

### Drift

- After both PRs land, `vendor/mngr` in this template lacks `apps/minds_workspace_server/` while `~/utilities/mngr` (post-deletion) also lacks it; in the in-between window where only the template PR has landed, the two diverge by exactly that one directory deletion.
- No documentation of this drift is added; future contributors performing a `git subtree pull` must reconcile manually.
