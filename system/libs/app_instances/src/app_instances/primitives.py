import re
from typing import Any, Final, Self

from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from app_instances.errors import InvalidInstanceValueError

# The instance-key rule of the workspace app model (contracts.md section 1): unique within
# its app, never changing, and drawn from a URL-safe alphabet so a key rides addresses,
# URL paths, and JSON keys unencoded.
MAX_INSTANCE_KEY_LENGTH: Final[int] = 128
INSTANCE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_INSTANCE_KEY_LENGTH - 1}}}$"
)

# A key prefix leaves room for the ``-<N>`` suffix the allocator appends.
INSTANCE_KEY_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)
MAX_INSTANCE_KEY_PREFIX_LENGTH: Final[int] = 100

# An instance URL is a path under the app's origin (contracts.md section 4.1). It may carry
# the tab placeholder once; the shell substitutes the id of the tab that opens it.
MAX_INSTANCE_URL_LENGTH: Final[int] = 2048
TAB_PLACEHOLDER: Final[str] = "{tab}"

MAX_INSTANCE_TITLE_LENGTH: Final[int] = 256

# The one substitution a title template makes: the number of the allocated key.
TITLE_NUMBER_PLACEHOLDER: Final[str] = "{n}"

# How much of an offending value an error message quotes.
_ERROR_VALUE_PREVIEW_LENGTH: Final[int] = 80


@pure
def _preview(value: str) -> str:
    return repr(value[:_ERROR_VALUE_PREVIEW_LENGTH])


@pure
def describe_instance_url_problem(
    value: str, is_placeholder_allowed: bool
) -> str | None:
    """Return why ``value`` cannot be an instance URL (or a location path), or None when it can."""
    if not value.startswith("/") or value.startswith("//"):
        return f"invalid path {_preview(value)}: must be rooted with a single slash"
    if len(value) > MAX_INSTANCE_URL_LENGTH:
        return f"invalid path: {len(value)} characters is over the {MAX_INSTANCE_URL_LENGTH}-character limit"
    if any(character < " " or character == "\x7f" for character in value):
        return f"invalid path {_preview(value)}: control characters are not allowed"
    placeholder_count = value.count(TAB_PLACEHOLDER)
    if placeholder_count > 1:
        return f"invalid path {_preview(value)}: the {TAB_PLACEHOLDER} placeholder may appear at most once"
    if placeholder_count == 1 and not is_placeholder_allowed:
        return f"invalid path {_preview(value)}: the {TAB_PLACEHOLDER} placeholder is not allowed here"
    return None


class InstanceKey(str):
    """An app-scoped instance identifier: URL-safe, one to 128 characters, fixed for the instance's life."""

    def __new__(cls, value: str) -> Self:
        if not INSTANCE_KEY_PATTERN.match(value):
            raise InvalidInstanceValueError(
                f"invalid instance key {_preview(value)}: keys match {INSTANCE_KEY_PATTERN.pattern}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class InstanceKeyPrefix(str):
    """The prefix of allocated keys (``<prefix>-<N>``): the key alphabet, with room left for the number."""

    def __new__(cls, value: str) -> Self:
        if not INSTANCE_KEY_PREFIX_PATTERN.match(value):
            raise InvalidInstanceValueError(
                f"invalid key prefix {_preview(value)}: prefixes match {INSTANCE_KEY_PREFIX_PATTERN.pattern}"
            )
        if len(value) > MAX_INSTANCE_KEY_PREFIX_LENGTH:
            raise InvalidInstanceValueError(
                f"invalid key prefix: {len(value)} characters is over the {MAX_INSTANCE_KEY_PREFIX_LENGTH}-character limit"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class InstanceUrl(str):
    """Where an instance's page is: a rooted path under the app's origin, optionally carrying ``{tab}`` once."""

    def __new__(cls, value: str) -> Self:
        problem = describe_instance_url_problem(value, is_placeholder_allowed=True)
        if problem is not None:
            raise InvalidInstanceValueError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class LocationPath(str):
    """A path a page reported it is at: the instance URL rule without the placeholder."""

    def __new__(cls, value: str) -> Self:
        problem = describe_instance_url_problem(value, is_placeholder_allowed=False)
        if problem is not None:
            raise InvalidInstanceValueError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class InstanceTitle(str):
    """What users see for an instance: non-blank, whitespace-trimmed, at most 256 characters."""

    def __new__(cls, value: str) -> Self:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidInstanceValueError("invalid title: must not be blank")
        if len(trimmed) > MAX_INSTANCE_TITLE_LENGTH:
            raise InvalidInstanceValueError(
                f"invalid title: {len(trimmed)} characters is over the {MAX_INSTANCE_TITLE_LENGTH}-character limit"
            )
        return super().__new__(cls, trimmed)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class TitleTemplate(str):
    """A title with a ``{n}`` placeholder for the allocated key's number, such as ``File Viewer {n}``."""

    def __new__(cls, value: str) -> Self:
        if TITLE_NUMBER_PLACEHOLDER not in value:
            raise InvalidInstanceValueError(
                f"invalid title template {_preview(value)}: must contain {TITLE_NUMBER_PLACEHOLDER}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


@pure
def render_title_template(template: TitleTemplate, number: int) -> InstanceTitle:
    """The title of the instance numbered ``number``: the template with its placeholder filled in."""
    return InstanceTitle(template.replace(TITLE_NUMBER_PLACEHOLDER, str(number)))
