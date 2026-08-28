"""Import roots allowed in the redirector's Modal container.

See the matching module in apps/remote_service_connector for the full
rationale: the import-boundary test derives the allowed third-party import
roots from this set, and a drift test ties it to the pyproject image group.
"""

from typing import Final

# ``modal`` is deliberately absent -- only the entrypoint (app.py) may import
# it. pydantic ships as a hard dependency of fastapi.
THIRD_PARTY_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "fastapi",
        "pydantic",
        # Consumed via imbue.modal_app_kit.sentry (error reporting to the
        # dev/ci Bugsink instance), not imported by the shipped modules
        # directly.
        "sentry_sdk",
    }
)
