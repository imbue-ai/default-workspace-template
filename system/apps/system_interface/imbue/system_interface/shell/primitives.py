import re
import secrets
from enum import auto
from typing import Any
from typing import Final
from typing import Self

from app_instances.primitives import InstanceKey
from app_manifest.primitives import AppName
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidAddressError
from imbue.system_interface.shell.errors import InvalidShellValueError

# The address grammar of contracts.md section 1: ``app:<name>`` names a single-instance app,
# ``app:<name>?instance=<key>`` one instance of an app.
ADDRESS_SCHEME: Final[str] = "app:"
ADDRESS_INSTANCE_PARAMETER: Final[str] = "instance="

# The unfiltered view: every instance of every app. A view id but never a project id.
EVERYTHING_VIEW_ID: Final[str] = "everything"
EVERYTHING_VIEW_NAME: Final[str] = "Everything"

_VIEW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
# A client id is the uuid the browser keeps in local storage (contracts.md section 1), and it
# names a layout file, so it is held to a filename-safe alphabet.
_CLIENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TAB_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^tab-[0-9a-f]{16}$")
_SAVE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^save-[0-9a-f]{16}$")
_MINTED_ID_BYTES: Final[int] = 8


def _string_schema(cls: type, handler: GetCoreSchemaHandler) -> CoreSchema:
    return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class Address(str):
    """The one way to name an instance: ``app:<name>`` or ``app:<name>?instance=<key>``."""

    def __new__(cls, value: str) -> Self:
        if not value.startswith(ADDRESS_SCHEME):
            raise InvalidAddressError(f"invalid address {value!r}: an address starts with {ADDRESS_SCHEME!r}")
        body = value[len(ADDRESS_SCHEME) :]
        name, separator, remainder = body.partition("?")
        try:
            AppName(name)
        except ValueError as e:
            raise InvalidAddressError(f"invalid address {value!r}: {e}") from e
        if separator:
            if not remainder.startswith(ADDRESS_INSTANCE_PARAMETER):
                raise InvalidAddressError(
                    f"invalid address {value!r}: the part after '?' must be {ADDRESS_INSTANCE_PARAMETER!r} plus a key"
                )
            try:
                InstanceKey(remainder[len(ADDRESS_INSTANCE_PARAMETER) :])
            except ValueError as e:
                raise InvalidAddressError(f"invalid address {value!r}: {e}") from e
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)

    @property
    def app(self) -> AppName:
        return AppName(self[len(ADDRESS_SCHEME) :].partition("?")[0])

    @property
    def key(self) -> InstanceKey | None:
        """The instance key, or None for a single-instance app's address."""
        remainder = self[len(ADDRESS_SCHEME) :].partition("?")[2]
        if not remainder:
            return None
        return InstanceKey(remainder[len(ADDRESS_INSTANCE_PARAMETER) :])

    def render(self) -> str:
        return str(self)


@pure
def address_for(app: AppName, key: InstanceKey | None) -> Address:
    if key is None:
        return Address(f"{ADDRESS_SCHEME}{app}")
    return Address(f"{ADDRESS_SCHEME}{app}?{ADDRESS_INSTANCE_PARAMETER}{key}")


class ViewId(NonEmptyStr):
    """A project id (the slugified project name) or the literal ``everything``."""

    def __new__(cls, value: str) -> Self:
        if not _VIEW_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid view id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class ProjectId(ViewId):
    """A view id that names a project: never the Everything view."""

    def __new__(cls, value: str) -> Self:
        if value == EVERYTHING_VIEW_ID:
            raise InvalidShellValueError(f"{EVERYTHING_VIEW_ID!r} is a view but not a project")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


@pure
def is_everything_view(view_id: str) -> bool:
    return view_id == EVERYTHING_VIEW_ID


class ClientId(NonEmptyStr):
    """One connected browser context, as its stored id names it."""

    def __new__(cls, value: str) -> Self:
        if not _CLIENT_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid client id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class TabId(NonEmptyStr):
    """A tab id the shell minted: ``tab-<16 hex>``, never reused."""

    def __new__(cls, value: str) -> Self:
        if not _TAB_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid tab id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


class SaveId(NonEmptyStr):
    """A layout save's id: ``save-<16 hex>``, minted by the window that saved."""

    def __new__(cls, value: str) -> Self:
        if not _SAVE_ID_PATTERN.fullmatch(value):
            raise InvalidShellValueError(f"invalid save id {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(cls, handler)


def mint_tab_id() -> TabId:
    return TabId(f"tab-{secrets.token_hex(_MINTED_ID_BYTES)}")


class ClientActivityKind(LowerCaseStrEnum):
    """What a client-activity report records: a message sent to an instance, or a view switch (a wire value)."""

    MESSAGE = auto()
    VIEW_SWITCH = auto()


class AppLifecycleAction(LowerCaseStrEnum):
    """The two verbs the workspace has for an app's supervised program (a wire value: the route's last path segment)."""

    STOP = auto()
    START = auto()


class DeviceKind(LowerCaseStrEnum):
    """Which arrangement a client saves into: desktop or mobile (a wire value)."""

    DESKTOP = auto()
    MOBILE = auto()
