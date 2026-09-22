"""Deploy-shape constants shared by the entrypoint and the shipped modules.

The import roots allowed in the connector's Modal container: the image pip set
itself lives in this app's ``[dependency-groups] image`` (pyproject.toml),
==-pinned and installed from the committed hash-locked
``image_requirements.txt`` export (regenerate with
``just export-image-requirements``). The import-boundary test derives the
allowed third-party import roots from ``THIRD_PARTY_IMPORT_ROOTS``, and a
drift test ties this set to the image group -- so the set of packages the
shipped code may import can never drift from what the container installs.

The web function's concurrency shape also lives here, because three places
must agree on it: the Modal decorator in app.py, the per-container DB pool in
db.py, and the sync-route thread limit web.py applies at startup.
"""

from typing import Final

# How many requests one container of the web function serves at once. Every
# route is a sync ``def`` that spends its time waiting on Neon, so the cap is
# generous; the autoscaler aims for the target and adds a container only when a
# burst exceeds it. The cap must clear one synchronized frps heartbeat burst,
# or the autoscaler adds a container per burst and retires it again once the
# scaledown window passes, and a retiring container can take in-flight
# requests down with it.
API_MAX_CONCURRENT_INPUTS: Final[int] = 32
API_TARGET_CONCURRENT_INPUTS: Final[int] = 16

# Sync routes run on anyio's default worker-thread limiter (40 tokens out of
# the box). It has to exceed the input cap, or requests admitted by Modal queue
# inside the container for a thread.
SYNC_ROUTE_THREAD_LIMIT: Final[int] = 64

# Import roots the shipped modules may use. Everything here must be provided
# by the image dependency group (directly, or as a hard dependency: pydantic
# ships with fastapi, anyio with starlette). ``modal`` is deliberately absent
# -- Modal injects its client into containers, but only the entrypoint
# (app.py) may import it.
THIRD_PARTY_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "acme",
        # Starlette's async backend; web.py reaches its thread limiter.
        "anyio",
        "boto3",
        "botocore",
        # A TTL cache with a real expiry and stampede protection.
        "cachetools",
        "cryptography",
        "fastapi",
        "httpx",
        "josepy",
        "jwt",
        "supertokens_python",
        "psycopg2",
        "paramiko",
        "pydantic",
        # Consumed via imbue.modal_app_kit.sentry (error reporting to the
        # tier's Bugsink instance), not imported by the shipped modules
        # directly.
        "sentry_sdk",
        "tenacity",
        # Two YAML-by-external-contract formats: electron-updater's channel
        # manifests (read by accounts_web; the shipped binary fixes their
        # format) and the cloud-init NoCloud material the mounted
        # ``imbue.mngr_imbue_cloud.slices.gen2_scripts.guest`` renders for the
        # gen-2 slice restore.
        "yaml",
    }
)
