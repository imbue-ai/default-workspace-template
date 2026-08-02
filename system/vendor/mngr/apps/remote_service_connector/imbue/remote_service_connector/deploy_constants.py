"""Single source of truth for what is installed in the connector's Modal image.

``app.py`` feeds ``PIP_INSTALLED_PACKAGES`` to ``Image.pip_install``, and the
import-boundary test derives the allowed third-party import roots from
``THIRD_PARTY_IMPORT_ROOTS`` -- so the set of packages the shipped code may
import can never drift from what the container actually has installed.
"""

from typing import Final

PIP_INSTALLED_PACKAGES: Final[tuple[str, ...]] = (
    "fastapi[standard]",
    "httpx",
    "supertokens-python",
    "psycopg2-binary",
    "paramiko",
    "tenacity",
)

# Import roots the shipped modules may use. Everything here must be provided
# by PIP_INSTALLED_PACKAGES (directly, or as a hard dependency: pydantic ships
# with fastapi). ``modal`` is deliberately absent -- Modal injects its client
# into containers, but only the entrypoint (app.py) is allowed to import it.
THIRD_PARTY_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "fastapi",
        "httpx",
        "supertokens_python",
        "psycopg2",
        "paramiko",
        "pydantic",
        "tenacity",
    }
)
