# modal_app_kit

Shared deploy-time conventions for our Modal apps (`apps/remote_service_connector`, `apps/modal_litellm`, `apps/oauth_redirector`), and the canonical documentation for **how our Modal services are structured and deployed, and why**.

The library itself is small and stdlib+modal only (with one documented exception, `sentry.py`, below):

- `deploy.py` -- readers for the deploy-time env vars (`MNGR_DEPLOY_ENV`, `MINDS_DEPLOY_ID`, warm-pool / scaledown knobs), the stamped Modal Secret naming (`<service>-<tier>-<deploy_id>`), and the inline deploy-metadata secret.
- `image.py` -- the pinned image inputs: the digest-pinned base image, the pinned in-build uv version, and `pinned_image` (the base + hash-locked install every service image starts from). The pure export machinery (the canonical `uv export` command, the pinned-app registry, and paths) lives in `imbue.imbue_common.modal_image_requirements`, kept in the public `imbue_common` (this package is not public) even though its remaining consumers are private, so the mirror's `imbue_common` stays self-contained.
- `source_mount.py` -- the rule for which local Python files ship into containers (`shipped_python_source_ignore`).
- `errors.py` -- the package's error hierarchy (`ModalAppKitError` and its subclasses).
- `database.py` -- `direct_database_url` (strips Neon's `-pooler` suffix for schema operations that are unsafe through transaction pooling).
- `request_logging.py` -- `RequestLoggingMiddleware`, a pure-ASGI middleware every app adds outermost: one single-line JSON record (`{"type": "http_request", ...}`) per HTTP request with method, path without the query string (it can carry one-time tokens), status, duration, client IP (derived exclusively from the ASGI socket peer -- Modal's ingress delivers the real end-client there and strips `X-Forwarded-For`, while other forwarding-style headers pass through unsanitized and are never consulted; see `client_ip_from_asgi_scope`), user agent, the `X-Imbue-Client` client id, and -- when available -- the authenticated user id and the deployed env's `minds_env` name, so Modal function logs carry a per-request record for abuse investigations and analytics. `json.dumps` keeps the client-controlled fields (the percent-decoded path, the user agent) escaped inside their JSON strings, so a crafted request cannot forge fields or lines in the log. Routes can shape their own line through two more scope-state keys: `ACCESS_LOG_SUPPRESS_SUCCESS_STATE_KEY` drops the line for 2xx responses only (high-frequency machine traffic -- the connector's frps heartbeats -- counted by periodic metric records instead; errors always log), and `ACCESS_LOG_PATH_OVERRIDE_STATE_KEY` replaces the logged path when the real one carries a credential in a path segment (the frps plugin-auth shared secret). Also exports `ensure_info_log_handler`, the handler bootstrap that makes a module logger's INFO lines reach the container's stderr as JSON regardless of the root logger's state (shared with `metrics.py`).
- `log_format.py` -- the one-line JSON log format every app's lines use, and `configure_logging`, the per-container bootstrap each Modal function calls first (next to `init_sentry`). Modal's OTEL exporter stamps every function-log line `level: INFO` in the tier's OpenObserve `modal_logs` stream and does not parse the content, so the only severity that survives is the one inside the line: `JsonLogFormatter` (the root handler) renders every record as `{"timestamp", "level", "logger", "type": "log", "message": ...}`, and `StructuredRecordJsonLogFormatter` (the dedicated handler `ensure_info_log_handler` installs on the structured-record loggers) flattens the JSON-object message into the same envelope instead (`http_request` / `metric` / `share_visit_authorized` keep their `type` and fields top-level, so consumers such as the analytics log views read them unchanged) -- the handler decides, never a sniff of the message text, so a plain log call whose text is a JSON object cannot forge a structured record; `minds_env` is stamped when deployed and tracebacks are folded into one `exception` string (Modal makes each stdout line its own record). Query the level with `spath(body, 'level')` (OpenObserve's JSON-string extraction function). Without `configure_logging` a container has no root handler at all -- Python drops INFO and prints WARNING+ as the bare message -- so the bootstrap installs one JSON handler on the root logger with third-party libraries held at WARNING and our `imbue.*` packages at INFO, overridable per deploy via `MINDS_LOG_LEVEL` (threaded in by the deploy metadata secret when the deployer exports it, e.g. `MINDS_LOG_LEVEL=DEBUG uv run minds-admin env deploy` on a dev env). It is deliberately an explicit entrypoint call rather than an import-time side effect: the shipped modules are also imported by unit tests, whose log capture a root handler would disturb.
- `sentry.py` -- shared sentry-sdk initialization for the apps reporting to the per-tier self-hosted Bugsink instances (see `specs/minds-bugsink-error-tracking.md`): `init_sentry` (idempotent per container; a no-op without a DSN or with `MINDS_SENTRY_DISABLED=1`; stdlib `logging` records at WARNING+ become events -- warning is the lower-priority channel for tolerated failures, error the top-priority one), a client-side dedup/rate-limit `before_send` hook, `capture_unexpected_exception` (explicit capture returning the event id, for app-level 500 handlers), and `capture_and_reraise` for Modal cron/spawned functions. The one exception to the stdlib+modal rule: this module imports `sentry_sdk`, so any app importing it MUST pin `sentry-sdk` in its image dependency group (enforced by the per-module allowance in `test_project_ratchets.py`).
- `metrics.py` -- `emit_metric`: one single-line JSON record (`{"type": "metric", "name": ..., "value": ..., "tags": {...}}`) per call, logged to the container's stderr so Modal's workspace-level OTEL integration ships it into the tier's OpenObserve `modal_logs` stream. For expected, routine anomalies (transient upstream errors, client-input junk) whose RATE is the interesting signal -- counted there instead of reported to Bugsink.

## The deployment model

Every service here is a plain Modal app deployed **by file path**:

```bash
MNGR_DEPLOY_ENV=<tier> MINDS_DEPLOY_ID=<id> uv run modal deploy --name <app> --env <modal-env> path/to/app.py
```

(normally driven by `minds-admin env deploy`, which threads the env vars and picks the Modal environment; see `apps/minds/docs/deploy/reference/environments.md`).

What ends up in the container is exactly three things:

1. **The image**: `pinned_image(...)` -- the digest-pinned `python:3.12-slim-trixie` base plus the app's hash-locked pip set (plus any build steps like prisma codegen). Built once and cached; rebuilt only when the pinned inputs / build steps change, and byte-reproducible when it does (see "Every image input is pinned" below).
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

- shipped modules import only stdlib + the pip set (the allowed import roots live in the app's `deploy_constants.py`, and a drift test ties them to the pyproject image group the image installs, so the two cannot drift) + shipped packages;
- no shipped module imports the `app` entrypoint;
- only the entrypoint imports `modal` (Modal injects its client into containers, but deployment concerns stay in one file);
- this library stays stdlib+modal only, since it ships into every consumer's container -- with the single per-module allowance for `sentry.py`'s `sentry_sdk` import described above (consumers of that module pin `sentry-sdk` in their image groups).

### Every image input is pinned

A rebuilt image must be a pure function of the repo state, never of when the build happens to run -- both for reproducibility and so we never silently adopt a just-published (possibly malicious) package release. Three mechanisms, all in `image.py`:

- **The pip set is hash-locked**: each app declares its exact image packages, `==`-pinned, in its own `[dependency-groups] image` (pyproject.toml). The group resolves inside the workspace `uv.lock` -- so unit tests exercise the same package versions the container ships, and the root `[tool.uv] exclude-newer` supply-chain cooldown applies to image packages for free. `just export-image-requirements` renders the group (with transitive pins and sha256 hashes, via the canonical command in `imbue.imbue_common.modal_image_requirements`) to a committed `image_requirements.txt` next to each app, and `pinned_image` installs it with `--require-hashes`, so the build can only ever install the exact reviewed artifacts.
- **The base is digest-pinned**: `PINNED_BASE_IMAGE` names `python:3.12-slim-trixie` by digest (same base family as the workspace template, same Python minor as the repo). Digest pins freeze security patches too -- bump the digest deliberately during dependency maintenance.
- **The installer is pinned**: `uv_pip_install(uv_version=...)` so Modal's default uv can't drift under us.

Enforcement: per-app drift tests fail when a committed export no longer matches `uv.lock` (or when the group and the allowed import roots disagree), `minds-admin env deploy` refuses to deploy a stale export, and the `test_prevent_unpinned_modal_pip_install` ratchet flags any new bare-package `pip_install`/`uv_pip_install`.

Known residual gap: the litellm image's `prisma generate` build step downloads Prisma engine binaries (and a Node runtime) from Prisma's CDN. The pinned `prisma` version determines *which* versions are fetched, but the downloads themselves are not hash-verified by us.

### Module-load env reads are deploy-time configuration

Values read from `os.environ` at the app module's top level (`read_deploy_env()`, `read_deploy_id()`, min-containers, scaledown) are evaluated when `modal deploy` imports the module and are **baked into the deployed function spec**. That is the mechanism `minds-admin env deploy` uses to configure a deploy, and it is why `modal app rollback` restores not just code but the previous deploy's configuration -- including which stamped Secrets it pins.

### Stamped secrets

Every Vault-backed secret is pushed as `<service>-<tier>-<deploy_id>` and the app pins the exact stamped names at deploy time (`stamped_secret`). When `MINDS_DEPLOY_ID` is unset, `read_deploy_id` returns a sentinel that matches no real secret, so a bare `modal deploy` outside `minds-admin env deploy` fails with "Secret not found" instead of silently attaching to stale secrets -- the property the rollback model relies on.

## Operational caveats

- **Rolling deploys drain old containers**: immediately after a redeploy, a warm container from the previous version may serve a few more requests. `minds-admin env deploy` manages this with deploy strategies, and `minds-admin env recover` force-terminates containers after a rollback.
- **First deploy builds the image** (~10s for the connector); subsequent code-only deploys skip the build entirely.

## Adding a new Modal service

1. Put the entrypoint `app.py` at the root of a regular package (so tests can import the code normally), and keep it Modal-only: image, `modal.App`, secrets, function definitions.
2. Declare the ==-pinned pip set in the app's `[dependency-groups] image`, register the package in `IMAGE_PINNED_PACKAGE_NAMES` (`imbue.imbue_common.modal_image_requirements`), run `just export-image-requirements`, and commit the resulting `image_requirements.txt`. Keep the allowed import roots in a `deploy_constants.py` consumed by the import-boundary test.
3. Start the image with `pinned_image(<path to the committed export>)` and end it with one `add_local_python_source("<your package>", "imbue.modal_app_kit", ignore=shipped_python_source_ignore)`.
4. Copy the `test_project_ratchets.py` boundary tests and the `image_requirements_drift_test.py` from `apps/remote_service_connector` and adjust the allowed roots.
5. Read the deploy-time knobs through `imbue.modal_app_kit.deploy`, never `os.environ` directly, so the sentinel/rollback semantics stay uniform.
6. Call `configure_logging()` (then `init_sentry`) as the first statement of every Modal function, so the container's lines are level-queryable JSON and errors are reported (the copied `test_every_modal_function_bootstraps_logging_first` guard fails on a function that does not). Name the entrypoint's own logger explicitly (`logging.getLogger("imbue.<your package>.app")`), never `__name__`: in the container the entrypoint is module `app`, outside the `imbue.*` subtree the bootstrap opens to INFO, so a `__name__` logger's INFO lines would be dropped (the copied `test_entrypoint_logger_is_named_under_imbue` guard fails on one).

See also: `specs/split_remote_service_connector_app.md` (the design that established this model and the prototype that validated it).
