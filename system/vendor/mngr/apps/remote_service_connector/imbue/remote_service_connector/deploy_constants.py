"""Import roots allowed in the connector's Modal container.

The image pip set itself lives in this app's ``[dependency-groups] image``
(pyproject.toml), ==-pinned and installed from the committed hash-locked
``image_requirements.txt`` export (regenerate with
``just export-image-requirements``). The import-boundary test derives the
allowed third-party import roots from ``THIRD_PARTY_IMPORT_ROOTS``, and a
drift test ties this set to the image group -- so the set of packages the
shipped code may import can never drift from what the container installs.
"""

from typing import Final

# Import roots the shipped modules may use. Everything here must be provided
# by the image dependency group (directly, or as a hard dependency: pydantic
# ships with fastapi). ``modal`` is deliberately absent -- Modal injects its
# client into containers, but only the entrypoint (app.py) may import it.
THIRD_PARTY_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "acme",
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
