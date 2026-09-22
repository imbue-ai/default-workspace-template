import re
from typing import Final
from typing import Self

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError

# An analyst name becomes both a Postgres role (``analyst_<name>``) and a
# Cloudflare token name (``analytics-analyst-<name>-<lake>-ro``), so it is kept
# to lowercase alphanumerics and underscores with no leading digit. The length
# cap keeps the derived role name comfortably under Postgres's 63-byte
# identifier limit.
ANALYST_NAME_PATTERN: Final[str] = r"[a-z][a-z0-9_]{1,31}"

# The provider instance the slice bake creates the workspace container under
# (a ``[providers.*]`` section of the default-workspace-template's ``.mngr/settings.toml``).
SLICE_PROVIDER_INSTANCE_NAME: Final[str] = "imbue_cloud_slice"

# ``pool_hosts.leased_to_user`` and the owner half of a backup bucket name
# (``<prefix>--<slug>``) hold the first 16 hex characters of the SuperTokens
# user id that ``workspace_records.user_id`` holds in full.
_USER_ID_PREFIX_LENGTH: Final[int] = 16


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


@pure
def derive_user_id_prefix(user_id: str) -> str:
    """The 16-hex prefix of a SuperTokens user id (dashes dropped), derived exactly as the connector does."""
    return user_id.replace("-", "")[:_USER_ID_PREFIX_LENGTH]
