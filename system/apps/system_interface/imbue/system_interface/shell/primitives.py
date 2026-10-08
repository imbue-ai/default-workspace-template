import re
import secrets
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema
from workspace_layout.primitives import WindowId

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.system_interface.shell.errors import InvalidShellValueError

_SAVE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^save-[0-9a-f]{16}$")
_MINTED_ID_BYTES: Final[int] = 8

# A desktop's ``glyph`` indexes the frontend's squiggle table.
GLYPH_COUNT: Final[int] = 10


def _string_schema(cls: type, handler: GetCoreSchemaHandler) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class SaveId(NonEmptyStr):
    """A layout save's id: ``save-<16 hex>``, minted by the window that saved."""

    def __new__(cls, value: str) -> Self:
        if not _SAVE_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid save id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


def mint_save_id() -> SaveId:
    """A save id for a write the shell makes itself (a window mints its own)."""
    return SaveId(f"save-{secrets.token_hex(_MINTED_ID_BYTES)}")


class AppLifecycleAction(LowerCaseStrEnum):
    """The two verbs the workspace has for an app's supervised program (a wire value: the route's last path segment)."""

    STOP = auto()
    START = auto()


def mint_window_id() -> WindowId:
    return WindowId(f"win-{secrets.token_hex(_MINTED_ID_BYTES)}")


class LaunchTargetKind(LowerCaseStrEnum):
    """Where a launch's page goes (post-launch-paths plan section 3.3): a new window, a window already at that path
    (else a new one), or a named window this client points at it (a wire value)."""

    NEW = auto()
    FOCUS = auto()
    WINDOW = auto()
