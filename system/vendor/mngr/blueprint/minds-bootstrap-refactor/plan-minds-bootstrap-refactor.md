# Plan: minds bootstrap refactor

## Overview

- `apps/minds/imbue/minds/bootstrap.py` mixes env-var translation, desired-state settings reconciliation, legacy migrations, and provider CRUD in one module; most of its comments exist to explain that structure. This refactor separates the concerns so the code documents itself.
- The load-bearing constraint: mngr reads settings.toml during its own import-time initialization (plugin blocking, config discovery), so all settings mutation must complete before any `imbue.mngr.*` import. Both the slimmed `bootstrap.py` and the new `mngr_settings` package therefore stay mngr-free, enforced statically (import-linter) and at runtime (`sys.modules` assertion).
- Behavior is pinned by a golden characterization test added before any restructuring. The only intentional behavior changes are the hard failure on invalid `MINDS_ROOT_NAME`, style-guide log-level corrections, and the new write lock.
- The riskiest logic — the hand-mirrored idempotency check inside `_ensure_mngr_settings` — becomes a declarative desired-state merge with a single `USER_OWNED_FIELDS` declaration, so the check and the write can never drift apart again.
- Delivered on a new branch off the current one (`mngr/bootstrap-refactor`) as one draft PR with one commit per step. Implementation starts only after explicit go-ahead.

## Expected behavior

- Unchanged: every minds CLI invocation still ensures settings before mngr loads (`main.py` runs `apply_bootstrap()`, then the `mngr_settings` ensure, then imports `cli_entry`).
- Unchanged: the reconciled settings.toml content — suppression blocks, recursive-plugin disable, Modal block, per-account imbue_cloud blocks, BYOK account blocks, panel enable/disable, signin/signout effects — is structurally identical to today (asserted by the golden test).
- Unchanged: `MINDS_ROOT_NAME` unset still means production defaults where production defaults apply today, and `apply_bootstrap` still leaves `MNGR_*` untouched in unactivated shells.
- Changed: a set-but-invalid `MINDS_ROOT_NAME` (e.g. stale `devminds`) now prints one clean error line — instructing `unset MINDS_ROOT_NAME`, then `eval "$(minds env activate <name>)"` — and exits 1, instead of warning and silently coercing to production.
- Changed: `is_minds_root_name_set_to_active_env()` becomes `is_env_activated()` with simplified semantics: unset returns False, invalid raises.
- Changed: concurrent settings writers (signin threads, startup reconcile, providers-panel toggles, BYOK create/delete) serialize on an flock, eliminating the last-writer-wins race.
- Changed: library-layer log statements drop from `info` to `debug` per the style guide, so `minds run` output gets slightly quieter.
- Changed: the dev-env-name suffix pattern is uniformly `{0,34}` (bootstrap.py previously allowed `{0,33}`; `environments.md` documented `{0,38}`).

## Changes

### bootstrap.py (slimmed to the pre-import layer)

- Keeps only: `MINDS_ROOT_NAME` constants and pattern (now the single source of the env-name suffix regex, imported by `envs/primitives.py`), resolve/validate functions, `env_name_from_root_name` / `root_name_for_env_name`, path/prefix helpers, `apply_bootstrap()` (env-var export only), `BootstrapError`, and the new `MindsRoot` class.
- `MindsRoot`: a plain hand-rolled immutable class (no pydantic, no dataclass) holding the resolved root name and derived paths; constructed once per process and passed explicitly everywhere.
- Stays stdlib+loguru only; import-linter forbids `imbue.mngr*`, pydantic, and click here.

### New package: imbue/minds/mngr_settings/

- Submodules: `interfaces.py` (store interface), a file-backed store implementation module, `data_types.py` (`CloudAccountSummary` and friends), `reconcile.py` (declarative desired-state ensure), `imbue_cloud_accounts.py`, `byok_accounts.py`, `_migrations.py`, `errors.py`-equivalent (`MindsSettingsError(BootstrapError)` — `minds/errors.py` is unusable here because it pulls click/mngr).
- Must never import `imbue.mngr*` or click (import-linter contract); pydantic/imbue_common are allowed (mngr loads pydantic immediately after anyway).
- The ensure entry point asserts no `imbue.mngr` module is in `sys.modules` yet, raising if the ordering invariant is violated.
- All public functions take a required `MindsRoot` parameter — the `root_name: str | None = None` resolve-if-None dance is gone. `minds run` resolves once into app state; CLI commands resolve at entry.
- `MindsSettingsStoreInterface` (MutableModel + ABC) with one file-backed implementation: read / update-via-mutator returning a modified flag, atomic tmp+rename write, and an flock-based inter-process lock around read-modify-write.
- Reconciliation becomes declarative: desired blocks are computed as data, `USER_OWNED_FIELDS = {"is_enabled"}` is declared once, and a generic merge preserves user-owned fields and reports whether anything changed. No hand-mirrored check.
- Legacy cleanup moves to `_migrations.py` as named idempotent functions run on every ensure (ssh-provider block removal, `dynamic_hosts.toml` + leased-key deletion, ambient `aws-*` purge, stale Modal `is_persistent=false` fix). No retirement criteria in docstrings.
- `slugify_account` remains an inlined copy, documented as a deliberate mirror of the plugin's version (no new dependency on `imbue-mngr-imbue-cloud`).
- `list_cloud_account_providers` returns `CloudAccountSummary` FrozenModels; `api_v1.py` reuses or maps them.
- Comments and docstrings are written well as the code moves — no dedicated pass, no special layout convention; the known inaccuracies (Modal sizing claim, stale aws-region comment) die with the rewritten code.

### Entry point and callers

- `main.py`: `apply_bootstrap()` (env vars only) → `mngr_settings` ensure → `import cli_entry`; catches `BootstrapError` and prints the clean one-line fix + exit 1.
- Callers updated to the new package and `MindsRoot` signatures: `cli/run.py`, `cli/env.py`, `desktop_client/supertokens_routes.py`, `desktop_client/api_v1.py`, `desktop_client/app.py`, `desktop_client/desktop_control.py`, `desktop_client/workspace_record_store.py`, `envs/paths.py`, `envs/docker_cleanup.py`, `envs/mngr_agent_cleanup.py`, `utils/sentry/core.py`, `testing.py`, deployment-test helpers.

### Tests

- Golden characterization test added first (integration-style, `test_mngr_settings.py`), snapshotting the parsed structure of the resulting settings.toml (not raw text) across: mngr-uninitialized, fresh-profile, legacy-ridden, already-desired-shape (asserting no write happens), user-disabled providers (asserting `is_enabled` survives), plus signin/signout and BYOK create/delete flows. Kept green through all steps; updated only for the intentional hard-fail change.
- Existing `bootstrap_test.py` tests redistributed to mirror the new module layout; store gets its own unit tests using real temp dirs (no mocks).
- Ratchet counts tightened where the refactor reduces violations (e.g. removed default arguments).

### Docs and small fixes (standalone commit)

- `environments.md` regex corrected to `{0,34}`.
- `desktop_client/README.md`: "FastAPI" → Flask.
- `DevEnvName` docstring/error-message length mismatch fixed.
- apps/minds changelog entry for the PR.

### Commit sequence

0. Golden characterization test.
1. Module split + import-linter contracts + runtime ordering guard.
2. Store interface/implementation + flock + `MindsRoot` + `CloudAccountSummary`.
3. Declarative reconcile (replaces the hand-mirrored check+write).
4. Migration quarantine into `_migrations.py`.
5. Hard-fail on invalid `MINDS_ROOT_NAME` + `is_env_activated` rename.
6. Regex unification + doc-bug fixes + changelog.

### Verification

- `just test-quick apps/minds` while iterating; full `just test-offload` before finishing; CI acceptance tests on the draft PR.
- Manual smoke check against the existing `dev-josh-1` env: activate, `minds run`, confirm settings.toml is written correctly and startup is clean.

### Explicitly out of scope

- Step 7 (upstream mngr changes: per-backend `auto_instantiate` opt-out, connector-url-less quiet skip).
- Typed provider-block construction from plugin config models (dropped — would require new plugin dependencies).
- Recording the one-sentence-per-line comment convention in any style guide.
