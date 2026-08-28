import re
from typing import Final
from typing import Self

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import MindError

# An analyst name becomes both a Postgres role (``analyst_<name>``) and a
# Cloudflare token name (``analytics-analyst-<name>-<lake>-ro``), so it is kept
# to lowercase alphanumerics and underscores with no leading digit. The length
# cap keeps the derived role name comfortably under Postgres's 63-byte
# identifier limit.
ANALYST_NAME_PATTERN: Final[str] = r"[a-z][a-z0-9_]{1,31}"


class InvalidAnalystNameError(MindError):
    """Raised when an analytics analyst name fails validation."""


class AnalystName(NonEmptyStr):
    """Short handle identifying one analytics analyst (e.g. ``josh``)."""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if not re.fullmatch(ANALYST_NAME_PATTERN, stripped):
            raise InvalidAnalystNameError(
                f"Invalid analyst name {value!r}: must match {ANALYST_NAME_PATTERN!r} "
                "(2-32 chars, lowercase alphanumerics/underscores, starting with a letter). "
                "Example: ``josh`` or ``alice_w``."
            )
        return super().__new__(cls, stripped)
