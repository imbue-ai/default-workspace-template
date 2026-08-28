"""Import roots allowed in the analytics app's Modal container.

The image pip set itself lives in this app's ``[dependency-groups] image``
(pyproject.toml), ==-pinned and installed from the committed hash-locked
``image_requirements.txt`` export (regenerate with
``just export-image-requirements``). The import-boundary test derives the
allowed third-party import roots from ``THIRD_PARTY_IMPORT_ROOTS``, so the
set of packages the shipped code may import can never drift from what the
container installs.
"""

from typing import Final

# Import roots the shipped modules may use. Everything here must be provided
# by the image dependency group. ``modal`` is deliberately absent -- Modal
# injects its client into containers, but only the entrypoint (app.py) may
# import it.
THIRD_PARTY_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "duckdb",
        "paramiko",
        "psycopg2",
        "pydantic",
    }
)
