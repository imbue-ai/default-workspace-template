# Plan: Pin the Modal service images

## Overview

- The two minds Modal service images (`apps/remote_service_connector`, `apps/modal_litellm`) install mostly-unpinned packages at build time (`pip_install("fastapi[standard]", ...)`), bypassing the workspace `uv.lock` and its `exclude-newer` supply-chain cooldown. Their base images (`debian_slim()`) are controlled by Modal, not by us.
- Converge on one version universe: the workspace `uv.lock` becomes the single version source for both the venv that tests run in and the deployed images. This also fixes an existing skew where tests run litellm 1.83.0 while the deployed proxy runs 1.93.0.
- Each app declares its exact image pip set as a dedicated `[dependency-groups] image = [...]` holding `==` pins; a committed, hash-locked requirements export generated from `uv.lock` (via `uv export --frozen --package <app> --only-group image`) is what the image installs. Hashes make builds byte-reproducible, and the root `exclude-newer` cooldown applies for free.
- Base images move from Modal-controlled `debian_slim()` to a digest-pinned `python:3.12-slim-trixie@sha256:...` (matching the repo's Python 3.12 and the workspace template's base), shared as one constant in `libs/modal_app_kit`.
- Drift tests, a new ratchet, and a deploy-time freshness check make "images install only from committed hash-locked exports" the enforced convention going forward.

## Expected behavior

- Image builds are deterministic: rebuilding either service image installs exactly the versions recorded in the committed export, verified by hash. Nothing about a rebuild depends on when it runs.
- Version changes only happen through a reviewable diff: bump the pin (or relock), re-run the `just` export recipe, review the export diff. Bumps are purely on-demand (litellm upgrade, CVE, etc.) — no scheduled cadence.
- Unit tests exercise the same package versions the containers ship (top-level and transitive), because both come from the same workspace resolution. The workspace litellm moves 1.83.0 -> 1.93.0 to match prod; if 1.93.0 postdates the root `exclude-newer` cutoff (2026-06-04), the cutoff is advanced forward-only as part of this change.
- CI fails via drift tests when: a committed export no longer matches `uv.lock`; the image group and `THIRD_PARTY_IMPORT_ROOTS` drift apart; or an image group entry loses its `==` pin.
- `minds env deploy` runs the same offline freshness check as a preflight and refuses to deploy a stale export (protects against deploys from stale checkouts or branches that skipped CI).
- The first deploy after this lands deliberately bumps prod package versions to the current `uv.lock` resolution and switches the base image — validated through the existing dev/CI-env deploys and `apps/minds/deployment_tests/` before staging/production.
- The uv binary that runs inside the image build is pinned (`uv_version=` on `uv_pip_install`), matching the repo's uv version, so the installer itself is not a floating build input.
- New unpinned `pip_install`/`uv_pip_install` calls with bare package names trip a per-project ratchet; the service apps' ratchets start at 0, and `libs/mngr_modal`'s built-in images are counted as existing violations to burn down later.
- Residual gap (documented, accepted): litellm's `prisma generate` build step still fetches Prisma engines and a Node runtime from Prisma's CDN; pinning `prisma==X` makes those fetches version-determined but not hash-verified by us.

## Changes

- `apps/remote_service_connector`:
  - Add an `image` dependency group to `pyproject.toml` with `==` pins for the image pip set (fastapi[standard], httpx, supertokens-python, psycopg2-binary, paramiko, tenacity); main dependencies keep their `>=` ranges.
  - Commit the hash-locked export at `apps/remote_service_connector/image_requirements.txt`.
  - `app.py`: build the image from the shared digest-pinned base + `uv_pip_install(requirements=[...], extra_options="--require-hashes", uv_version=<pinned>)` instead of `debian_slim().pip_install(*PIP_INSTALLED_PACKAGES)`.
  - `deploy_constants.py`: drop `PIP_INSTALLED_PACKAGES` (the image group is now the source of truth); keep `THIRD_PARTY_IMPORT_ROOTS`.
  - Drift tests: image group <-> committed export <-> `uv.lock` consistency, group names <-> allowed import roots, `==`-pin enforcement.
- `apps/modal_litellm`: same mechanism — `image` group pinning `litellm[proxy]==1.93.0`, `prisma==X`, `pyyaml==X`, `tenacity==X`; committed export; digest-pinned base; drift tests. Re-verify the pricing/config drift tests and the budget-enforcement behavior called out in the existing litellm pin comment after the workspace bump.
- `libs/modal_app_kit`: the shared digest-pinned base-image constant (commented, bumped by hand) and a helper for constructing the base image; update the README's deployment-model docs (image section) to describe the export-based rule.
- Root `pyproject.toml` / `uv.lock`: relock for the new groups and the litellm bump; advance `exclude-newer` forward-only if required.
- `justfile`: a recipe that regenerates both exports via `uv export --frozen --package <app> --only-group image` (the same command the drift tests replay offline).
- `apps/minds`: `minds env deploy` preflight gains the export-freshness check and refuses to deploy on mismatch.
- Ratchets (via the `/writing-ratchet-tests` conventions): per-project ratchet flagging `pip_install`/`uv_pip_install` calls with bare package names — service apps at 0, `libs/mngr_modal` counting its built-ins (default host image, snapshot route, example route).
- Changelog entries for every touched project (`apps/remote_service_connector`, `apps/modal_litellm`, `libs/modal_app_kit`, `libs/mngr_modal`, `apps/minds`, `dev` for justfile/root changes).
- Documented follow-ups (not in this change): mngr_modal built-in images (apt snapshot pinning, snapshot-route app), test-infra Dockerfiles (`libs/mngr/imbue/mngr/resources/Dockerfile`, mngr_minds_eval box), digest-pinning the workspace template's base tag, and socket.dev adoption on lockfile diffs.
