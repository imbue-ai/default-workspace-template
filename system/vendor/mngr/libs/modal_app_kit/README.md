# modal_app_kit

Shared deploy-time conventions for our Modal apps (`apps/remote_service_connector`, `apps/modal_litellm`), and the canonical documentation for **how our Modal services are structured and deployed, and why**.

The library itself is small and stdlib+modal only:

- `deploy.py` -- readers for the deploy-time env vars (`MNGR_DEPLOY_ENV`, `MINDS_DEPLOY_ID`, warm-pool / scaledown knobs), the stamped Modal Secret naming (`<service>-<tier>-<deploy_id>`), and the inline deploy-metadata secret.
- `source_mount.py` -- the rule for which local Python files ship into containers (`shipped_python_source_ignore`).
- `database.py` -- `direct_database_url` (strips Neon's `-pooler` suffix for schema operations that are unsafe through transaction pooling).

## The deployment model

Every service here is a plain Modal app deployed **by file path**:

```bash
MNGR_DEPLOY_ENV=<tier> MINDS_DEPLOY_ID=<id> uv run modal deploy --name <app> --env <modal-env> path/to/app.py
```

(normally driven by `minds env deploy`, which threads the env vars and picks the Modal environment; see `apps/minds/docs/environments.md`).

What ends up in the container is exactly three things:

1. **The image**: `debian_slim` + the app's `pip_install` set (plus any build steps like prisma codegen). Built once and cached; rebuilt only when the pip set / build steps change.
2. **The entrypoint file**: Modal's file-path deploy auto-mounts *only* `app.py`, at `/root/app.py`, imported in-container as top-level module `app`.
3. **The source mounts**: one trailing `add_local_python_source(...)` call listing the packages the app needs (e.g. `"imbue.remote_service_connector", "imbue.modal_app_kit"`), filtered by `shipped_python_source_ignore`.

Nothing else from the monorepo exists at runtime.

## The rules, and why each exists

### Deploy by file path; never `modal deploy -m`

Module-mode deploy mounts the *top-level package* of the dotted path. Our top-level package is `imbue`, a namespace package spanning every `libs/*` and `apps/*` project -- `-m` would upload essentially the whole monorepo. File mode mounts one file and leaves everything else to the explicit source mount.

### One `add_local_python_source` call, as the last image operation

- **Last**: with the default `copy=False`, local source is attached to containers at startup and is *not* an image layer, so code changes never invalidate the cached pip image; redeploys take a few seconds. Modal enforces the ordering (any build step after a non-copy `add_local_*` raises `InvalidError` at deploy).
- **One call**: `add_local_python_source` accepts multiple dotted module names, so a single operation ships every needed package and there is never a second place to keep in sync.
- Dotted names are resolved with `importlib` in the deploying interpreter (the uv workspace venv), so the call works regardless of the working directory and ships only that subpackage -- `"imbue.remote_service_connector"` mounts just the connector's own directory to `/root/imbue/remote_service_connector/`.

### `shipped_python_source_ignore`: only production `.py` files ship

The predicate keeps three classes of files out of the mount:

- **Non-`.py` files** (mirrors Modal's own default): `.pyc` / `__pycache__` and data files can never churn the upload set or the cache.
- **Test files** (`*_test.py`, `test_*.py`, `conftest.py`, `testing.py`): tests never ship.
- **The package-root `app.py`**: the entrypoint already ships via Modal's automatic file mount (as module `app`). Shipping a second copy inside the package would let an accidental `import <package>.app` execute the module twice under a different name (a second `modal.App`, a second FastAPI app); excluding it makes such an import fail loudly instead.

### The import boundary (and the tests that enforce it)

Because the container has only the pip set + the shipped packages, **shipped modules may import only: the stdlib, the pip-installed set, and the shipped packages themselves**. Any other import -- most dangerously, another monorepo package like `imbue.imbue_common` -- works locally (the venv has everything) and crashes the deployed container at import time.

This cannot be prevented structurally, so it is enforced by tests in each app (see `test_project_ratchets.py` in `apps/remote_service_connector` and in this library):

- shipped modules import only stdlib + the pip set (kept in the app's `deploy_constants.py`, the same constant the image `pip_install` uses, so the two cannot drift) + shipped packages;
- no shipped module imports the `app` entrypoint;
- only the entrypoint imports `modal` (Modal injects its client into containers, but deployment concerns stay in one file);
- this library stays stdlib+modal only, since it ships into every consumer's container.

### Module-load env reads are deploy-time configuration

Values read from `os.environ` at the app module's top level (`read_deploy_env()`, `read_deploy_id()`, min-containers, scaledown) are evaluated when `modal deploy` imports the module and are **baked into the deployed function spec**. That is the mechanism `minds env deploy` uses to configure a deploy, and it is why `modal app rollback` restores not just code but the previous deploy's configuration -- including which stamped Secrets it pins.

### Stamped secrets

Every Vault-backed secret is pushed as `<service>-<tier>-<deploy_id>` and the app pins the exact stamped names at deploy time (`stamped_secret`). When `MINDS_DEPLOY_ID` is unset, `read_deploy_id` returns a sentinel that matches no real secret, so a bare `modal deploy` outside `minds env deploy` fails with "Secret not found" instead of silently attaching to stale secrets -- the property the rollback model relies on.

## Operational caveats

- **Rolling deploys drain old containers**: immediately after a redeploy, a warm container from the previous version may serve a few more requests. `minds env deploy` manages this with deploy strategies, and `minds env recover` force-terminates containers after a rollback.
- **First deploy builds the image** (~10s for the connector); subsequent code-only deploys skip the build entirely.

## Adding a new Modal service

1. Put the entrypoint `app.py` at the root of a regular package (so tests can import the code normally), and keep it Modal-only: image, `modal.App`, secrets, function definitions.
2. Define the pip set once in a `deploy_constants.py` consumed by both the image and the import-boundary test.
3. End the image with one `add_local_python_source("<your package>", "imbue.modal_app_kit", ignore=shipped_python_source_ignore)`.
4. Copy the `test_project_ratchets.py` boundary tests from `apps/remote_service_connector` and adjust the allowed roots.
5. Read the deploy-time knobs through `imbue.modal_app_kit.deploy`, never `os.environ` directly, so the sentinel/rollback semantics stay uniform.

See also: `specs/split_remote_service_connector_app.md` (the design that established this model and the prototype that validated it).
