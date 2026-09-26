import re
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.system_interface.shell.errors import InvalidShellValueError

# A design's catalog id: lowercase, starts with a letter, at most 48 characters.
_DESIGN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


class DesignId(NonEmptyStr):
    """The catalog id of an avatar design, bundled or registered."""

    def __new__(cls, value: str) -> Self:
        if not _DESIGN_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid design id {value!r}: lowercase letters, digits, and dashes")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())
