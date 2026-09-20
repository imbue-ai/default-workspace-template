import urllib.parse
from typing import Any, Final, Self

from app_manifest.primitives import AppName
from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from browser.errors import InvalidBrowserNameValueError, InvalidStartUrlError
from browser.names import is_valid_browser_name

# The app's registered name (what its manifest, system/apps/browser/app.toml, declares and the
# supervisord program line registers): the fleet manifest under data/.apps is named by it.
APP_NAME: Final[AppName] = AppName("browser")

# The viewer page selects its browser by this query parameter (assets/index.html).
_SESSION_QUERY_KEY: Final[str] = "session"

# A start page is an absolute http(s) URL with a host; the length bound keeps a query string
# from smuggling in an unbounded value.
MAX_START_URL_LENGTH: Final[int] = 2048
_START_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_URL_PREVIEW_LENGTH: Final[int] = 80


class BrowserName(str):
    """A browser's name: lowercase alphanumeric words joined by single dashes, at most 40 characters, not all digits."""

    def __new__(cls, value: str) -> Self:
        if not is_valid_browser_name(value):
            raise InvalidBrowserNameValueError(
                f"invalid browser name {value!r}: names are lowercase letters, digits, and single dashes, 1 to 40 characters, not all digits"
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
def browser_page_path(name: BrowserName) -> str:
    """The viewer page for one browser: the app root with the browser selected by query."""
    return f"/?{_SESSION_QUERY_KEY}={name}"


@pure
def describe_start_url_problem(value: str) -> str | None:
    """Return why ``value`` cannot be a start page, or None when it can."""
    if len(value) > MAX_START_URL_LENGTH:
        return f"invalid url: {len(value)} characters is over the {MAX_START_URL_LENGTH}-character limit"
    if any(character <= " " or character == "\x7f" for character in value):
        return f"invalid url {_preview(value)}: whitespace and control characters are not allowed"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as e:
        return f"invalid url {_preview(value)}: {e}"
    if parts.scheme not in _START_URL_SCHEMES or not parts.netloc:
        return f"invalid url {_preview(value)}: expected an absolute http or https URL with a host"
    return None


@pure
def _preview(value: str) -> str:
    if len(value) <= _URL_PREVIEW_LENGTH:
        return repr(value)
    return repr(value[:_URL_PREVIEW_LENGTH] + "...")


class AbsoluteHttpUrl(str):
    """A browser's start page: an absolute http(s) URL with a host, at most 2048 characters, no whitespace or control characters."""

    def __new__(cls, value: str) -> Self:
        problem = describe_start_url_problem(value)
        if problem is not None:
            raise InvalidStartUrlError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )
