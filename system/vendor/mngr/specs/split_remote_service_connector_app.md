# Splitting remote_service_connector's app.py into modules

## Purpose

`apps/remote_service_connector/imbue/remote_service_connector/app.py` is a single 7400-line file.
It is one file because of a deployment constraint: the connector is deployed with `modal deploy <path>/app.py`, and Modal's file-path deploy mode ships *only that file* into the container (mounted at `/root/app.py`, imported as top-level module `app`).
The file's docstring states the resulting rule: "This file is entirely self-contained -- it has NO imports from the monorepo."

This spec documents the *validated* mechanism for shipping a multi-module package to Modal instead, so that app.py can be broken apart.
Every claim below was verified against Modal 1.4.3 (the version pinned in `pyproject.toml`), either by reading the SDK source in `.venv` or by deploying a minimal prototype to the `minds-dev` Modal workspace (see "Prototype record" below).
This spec deliberately does not prescribe the final module decomposition of app.py; it establishes the deployment mechanics that any decomposition must use.

## Requirements

The mechanism must satisfy all of the following:

1. Do not ship the whole monorepo (or anything beyond the connector's own package) into the image; uploads must stay small and fast.
2. No explicit per-file lists that must be remembered when adding a new module.
3. `.pyc` / `__pycache__` files must never cause image cache misses or spurious uploads.
4. Image construction must be sequenced so that a *single* operation at the end includes the local code, keeping all earlier (expensive) layers cacheable.
5. The existing deploy entrypoints must keep working: `minds env deploy` invokes `modal deploy --name rsc-<tier> --env <env> <abs path to app.py>` (see `apps/minds/imbue/minds/envs/per_env_deploy.py::_connector_app_file`), and module-load-time env reads (`MNGR_DEPLOY_ENV`, `MINDS_DEPLOY_ID`, `MINDS_CONNECTOR_MIN_CONTAINERS`, `MINDS_CONNECTOR_SCALEDOWN_WINDOW`) must still be captured at deploy-time serialization.

## The validated mechanism

### Keep deploying by file path, never by module

Modal has two entrypoint modes (`modal/_utils/function_utils.py::get_entrypoint_mount`):

- **File mode** (`modal deploy path/to/app.py`): mounts only that one file to `/root/app.py`; the container imports it as top-level module `app`.
- **Module mode** (`modal deploy -m imbue.remote_service_connector.app`): mounts the *top-level* package of the dotted path -- `imbue`.

Module mode is a trap in this monorepo: `imbue` is a namespace package whose `submodule_search_locations` span every `libs/*/imbue` and `apps/*/imbue` directory in the repo, so `-m` would upload essentially the whole monorepo.
File mode must stay.

### Ship the package with one `add_local_python_source` call as the last image operation

```python
image = (
    modal.Image.debian_slim()
    .pip_install("fastapi[standard]", "httpx", "supertokens-python", "psycopg2-binary", "paramiko", "tenacity")
    .add_local_python_source("imbue.remote_service_connector", ignore=_ignore_non_shipped_files)
)
```

Verified properties of `add_local_python_source` (Modal 1.4.3):

- **Dotted names work and ship only the subpackage.**
  `_MountedPythonModule` resolves the name via `importlib.util.find_spec` and mounts the package's own directory to `/root/imbue/remote_service_connector`.
  Because `imbue.remote_service_connector` is a regular package (it has `__init__.py`), its spec has exactly one search location -- the connector's own directory -- so nothing else in the `imbue` namespace ships.
- **Resolution is by import name, not by path.**
  `find_spec` runs in the deploying interpreter (the uv venv, where the package is installed editable), so the call works regardless of the deploy process's working directory.
- **The default ignore already excludes bytecode.**
  The default `ignore=NON_PYTHON_FILES` is `~FilePatternMatcher("**/*.py")`: only `.py` files ship.
  `.pyc`, `__pycache__`, and any non-Python file are excluded with no configuration (requirement 3).
- **New modules are picked up automatically.**
  The whole package directory is walked recursively; adding a file requires no registration anywhere (requirement 2).
- **Namespace packages work in the container.**
  `/root` is on `sys.path` and `/root/imbue/` has no `__init__.py`; Python's implicit namespace packages make `import imbue.remote_service_connector.<x>` work anyway (verified in the prototype, which used an `__init__`-less namespace root).
- **`copy=False` (the default) makes it a container-startup mount, not an image layer.**
  The image is exactly the `pip_install` chain and is untouched by code changes.
  Measured with the prototype: first deploy 12.2s (9.3s one-time image build), redeploy after a code change 2.3s with zero image build lines, no-op redeploy 1.8s.
  Mount uploads are content-addressed, so unchanged files are not re-uploaded (requirements 1 and 4).
- **Modal enforces the "last operation" rule.**
  Any build step (e.g. `run_commands`) after a non-copy `add_local_*` raises `InvalidError` at deploy time (`_Image._assert_no_mount_layers`), so the sequencing in requirement 4 cannot silently regress.

### Exclude test files and the entrypoint from the mount

The `ignore` parameter accepts a callable receiving paths *relative to the mounted package root* (e.g. `app.py`, `helpers/format.py`).
Compose the default only-`.py` rule with the repo's test-file conventions:

```python
_NON_PYTHON_FILES = ~modal.FilePatternMatcher("**/*.py")
_NON_SHIPPED_PYTHON_FILES = modal.FilePatternMatcher(
    "**/*_test.py",   # unit tests
    "**/test_*.py",   # integration/acceptance/release tests
    "**/conftest.py",
    "**/testing.py",
    "app.py",         # the entrypoint itself; see below
)


def _ignore_non_shipped_files(path: Path) -> bool:
    return _NON_PYTHON_FILES(path) or _NON_SHIPPED_PYTHON_FILES(path)
```

The patterns follow dockerignore syntax; a bare `app.py` matches only the package-root file, not nested ones.
This is a list of stable *patterns*, not a per-file list, so it does not violate requirement 2.

**Why exclude `app.py` from the package mount:** the entrypoint already ships via Modal's automatic file mount (`/root/app.py`, module name `app`).
Without the exclusion the same file would also exist as `/root/imbue/remote_service_connector/app.py`, and an accidental `import imbue.remote_service_connector.app` from a split-out module would *execute the module a second time* under a different name (second `modal.App`, second FastAPI app).
With the exclusion, such an import fails loudly in the container instead.
Local imports (tests, conftest) are unaffected -- they run in the venv, not from the mount.

The prototype verified the exclusions end-to-end: a package containing `core_test.py`, `test_integration.py`, `conftest.py`, `testing.py`, `data.json`, and pre-generated `__pycache__` directories uploaded exactly its four real source files.

### What the container looks like at runtime

- `/root/app.py` -- the entrypoint (auto file mount), imported as module `app`; defines the image, `modal.App`, secrets, `@app.function`s, and assembles the FastAPI app.
- `/root/imbue/remote_service_connector/...` -- the split-out modules (single `add_local_python_source` mount).
- `__pycache__` directories appear in-container after import; they are generated there, not uploaded.

A caveat observed during the prototype (pre-existing behavior, not introduced by the split): immediately after a rolling deploy, warm containers from the previous version briefly keep serving; `minds env deploy` already manages this via deploy strategies and `minds env recover`'s container termination.

## Consequences for the codebase

### The "self-contained" rule relaxes to a package-boundary rule

Today the single-file convention *physically* prevents monorepo imports.
After the split, an import of e.g. `imbue.imbue_common.*` from a connector module would work locally and in unit tests but crash the container at import time (`ModuleNotFoundError`), because only `imbue.remote_service_connector` ships.
The rule becomes: **connector modules may import stdlib, the pip-installed third-party set, and `imbue.remote_service_connector.*` -- nothing else from the monorepo.**

This must be enforced by a test (e.g. a project ratchet in `test_project_ratchets.py`, or an import-walking unit test) since nothing structural prevents it anymore.
The same test should assert that no module under the package imports `imbue.remote_service_connector.app` (the entrypoint is excluded from the mount, so such an import only fails at runtime in the container).

An alternative -- shipping `imbue_common` as a second `add_local_python_source` module -- was rejected: it drags in `loguru`/pydantic-model conventions and grows the shipped surface for no current need.
If genuinely shared code emerges later, add it as an explicit second dotted module name and extend the import-boundary test.

### Sketch of the decomposition (non-binding)

app.py stays as the Modal entrypoint and keeps everything that must be evaluated at deploy-time serialization: `_DEPLOY_ENV` / `_MINDS_DEPLOY_ID` / min-containers / scaledown env reads, the image definition, `modal.App`, secrets, `@app.function` / cron definitions, and the top-level FastAPI route registrations.
The bulk moves into the package, e.g.: `errors.py`, `html_pages.py`, `cloudflare_ops.py`, `stores.py` (Postgres stores), `auth_supertokens.py`, `leasing.py`, `buckets_r2.py`, `sync_records.py`, `entitlements.py`, `sweeps.py`.
Import direction: package modules never import `app`; `app` imports the package.
Tests continue importing `imbue.remote_service_connector.<module>` exactly as they import `...app` today, and can be split alongside the code.

The same mechanism applies verbatim to `apps/modal_litellm/app.py` (314 lines) if it ever needs splitting, though it is currently fine as one file.

### What does not change

- `minds env deploy`, `per_env_deploy.py`, and the README's direct `modal deploy` command: same file path, same env-var threading.
- Rollback semantics: the env vars baked at deploy time still fully determine the function spec; code arrives via content-addressed mounts tied to the deployment version.
- Unit tests keep importing the package through the venv; `conftest.py` and `testing.py` stay where they are (and are excluded from the mount).

## Alternatives considered and rejected

- **`modal deploy -m` (module mode):** ships the entire `imbue` namespace (whole monorepo). Rejected outright.
- **`add_local_dir(<pkg dir>, remote_path="/root/imbue/remote_service_connector")`:** equivalent runtime result, but resolves by filesystem path (fragile against invocation cwd) and its default ignore excludes nothing, so `__pycache__`/`.pyc`/data files must be excluded by hand. `add_local_python_source` has the right defaults and is keyed to the import name.
- **`copy=True` variants:** bake the code into an image layer; every code change rebuilds that layer (and would re-expose the `.pyc` cache-miss problem that the default only-`.py` ignore otherwise avoids). Only needed if build steps must run *after* the code is added, which is not the case here.
- **Building a wheel and `pip_install`ing it into the image:** slow, cache-busting on every change, and adds a packaging step to every deploy.

## Prototype record

The prototype lives only in the session scratchpad (it is throwaway); its essential content is reproduced here.
Layout: `app.py` next to a namespace root `imbue_proto/` (no `__init__.py`) containing regular package `connector_proto/` with a nested `helpers/` subpackage, plus decoy files (`core_test.py`, `test_integration.py`, `conftest.py`, `testing.py`, `data.json`, generated `__pycache__`).

The entrypoint's essential lines:

```python
import modal
from imbue_proto.connector_proto.core import build_core_payload

_NON_PYTHON_FILES = ~modal.FilePatternMatcher("**/*.py")
_TEST_FILES = modal.FilePatternMatcher("**/*_test.py", "**/test_*.py", "**/conftest.py", "**/testing.py")


def _ignore_non_shipped_files(path: Path) -> bool:
    return _NON_PYTHON_FILES(path) or _TEST_FILES(path)


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]")
    .add_local_python_source("imbue_proto.connector_proto", ignore=_ignore_non_shipped_files)
)
app = modal.App(name="split-proto", image=image)


@app.function(name="probe", timeout=60)
@modal.fastapi_endpoint()
def probe() -> dict[str, object]:
    # returned the imported payload plus os.walk("/root") to verify shipped files
    ...
```

Verification steps and results:

1. Local (no deploy): `modal.mount._MountedPythonModule("imbue_proto.connector_proto", ignore=...).get_files_to_upload()` listed exactly the four real `.py` files; none of the decoys, no `.pyc`.
2. `modal deploy --env split-proto-josh app.py` (profile `minds-dev`): 12.2s, image built once (9.3s), two mounts created (`app.py`, `PythonPackage:imbue_proto.connector_proto`).
3. The probe endpoint confirmed the in-container file set, working nested imports, `imbue_proto` as an `__init__`-less namespace package, and `__name__ == "app"`.
4. Changed a constant, regenerated `__pycache__` everywhere via `compileall`, redeployed: 2.3s, zero image-build output, new value served (after the previous version's warm container drained).
5. No-op redeploy: 1.8s.

Everything the prototype created in Modal was removed afterwards, and nothing else was touched: app `split-proto` (in the dedicated environment `split-proto-josh`, `minds-dev` workspace) was stopped, and the environment `split-proto-josh` was deleted (`modal environment delete` cascades the apps inside it).

## Implementation record

The split described here has been implemented (same branch as this spec).
The resolved decisions:

- The shared lib is `libs/modal_app_kit` (`imbue.modal_app_kit`), and its README is the canonical documentation for the deployment model.
- The decomposition added a `naming.py` foundation module below `auth.py` (tunnel naming is needed by both auth and forwarding), and R2 is an `r2/` subpackage (`naming`, `stores`, `buckets`, `grants`, `sweep`).
- `web.py` assembles the FastAPI app from per-feature `APIRouter`s and owns the system endpoints; `app.py` is Modal-only and is excluded from the source mount.
- `deploy_constants.py` is the single source for the image's pip set; `test_project_ratchets.py` enforces the import boundary, the no-entrypoint-import rule, the only-app-imports-modal rule, and the module-attribute seam convention. An import-linter layers contract covers the internal DAG.
- `app_test.py` was split into per-module `<module>_test.py` files in the same change; shared client factories moved to `testing.py`.
- Cross-module runtime seams are called through their owning module (`forwarding.get_ctx()`, `stores.get_key_store()`, ...) so a single monkeypatch on the owning module reaches every caller; the previously-private seams that crossed module boundaries were made public.
