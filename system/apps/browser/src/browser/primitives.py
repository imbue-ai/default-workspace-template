import re
from typing import Any, Final, Self

from app_instances.primitives import InstanceTitle, InstanceUrl
from app_manifest.primitives import AppName
from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from browser.errors import InvalidBrowserNameValueError
from browser.names import NUMBERED_NAME_STEM, is_valid_browser_name

# The app's registered name (what its manifest, system/apps/browser/app.toml, declares and the
# supervisord program line registers): the shell nudge and the instance store are named by it.
APP_NAME: Final[AppName] = AppName("browser")

# The names the daemon mints, whose titles derive back from the number ("Browser 3"), as the
# workspace UI has always shown them.
_NUMBERED_BROWSER_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{NUMBERED_NAME_STEM}-([0-9]+)$"
)

# The viewer page selects its browser by this query parameter (assets/index.html).
_SESSION_QUERY_KEY: Final[str] = "session"


class BrowserName(str):
    """A browser's name, which is its instance key: lowercase alphanumeric words joined by single dashes, at most 40 characters, not all digits."""

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
def derive_browser_title(name: BrowserName) -> InstanceTitle:
    """The title a browser wears: ``Browser 3`` for ``browser-3``, a legacy name verbatim."""
    match = _NUMBERED_BROWSER_PATTERN.fullmatch(name)
    if match is None:
        return InstanceTitle(name)
    return InstanceTitle(f"Browser {match.group(1)}")


@pure
def instance_url_for_browser(name: BrowserName) -> InstanceUrl:
    """The viewer page for one browser: the app root with the browser selected by query."""
    return InstanceUrl(f"/?{_SESSION_QUERY_KEY}={name}")
