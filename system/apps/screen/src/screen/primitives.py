import re
import urllib.parse
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from app_manifest.primitives import AppName
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from screen.browser_names import is_valid_browser_name
from screen.errors import InvalidBrowserNameError
from screen.errors import InvalidDroppedFileNameError
from screen.errors import InvalidRunCommandError
from screen.errors import InvalidStartUrlError
from screen.errors import WindowToolError

# The app's registered name (what its manifest, system/apps/screen/app.toml, declares and the
# supervisord program line registers).
APP_NAME: Final[AppName] = AppName("screen")
# The manifest's ``display_name`` and ``description``, which the API document repeats as its title and description.
APP_DISPLAY_NAME: Final[str] = "Screen"
APP_DESCRIPTION: Final[str] = (
    "The workspace's one streamed Linux desktop, a window the person or an agent drives, one at a time; the browser runs on it."
)

# The one display (docs/system/specs/screen/spec.md D16), taken from the browser's Xvfb.
DISPLAY: Final[str] = ":99"
DISPLAY_SOCKET_PATH: Final[str] = "/tmp/.X11-unix/X99"
# The members' XDG runtime dir, where the stack's own sockets live (PulseAudio's, the input driver's).
RUNTIME_DIR: Final[str] = "/tmp/screen-runtime"

# Where the service, neko and the ICE muxes listen (spec section 5.1), and the browser launch path's route (spec
# section 16), which the ``screen browser open`` command and the XFCE session's browser entries call.
SERVICE_HOST: Final[str] = "127.0.0.1"
SERVICE_PORT: Final[int] = 8090
LAUNCH_BROWSER_PATH: Final[str] = "/launch/browser"
NEKO_HOST: Final[str] = "127.0.0.1"
NEKO_PORT: Final[int] = 8091
ICE_MUX_PORT: Final[int] = 59000

# The supervisord group of the stack and its members, in the order the service starts them.
STACK_GROUP: Final[str] = "screen-stack"
XORG_PROGRAM: Final[str] = "screen-xorg"
PULSE_PROGRAM: Final[str] = "screen-pulse"
SESSION_PROGRAM: Final[str] = "screen-session"
NEKO_PROGRAM: Final[str] = "screen-neko"
STACK_PROGRAMS: Final[tuple[str, ...]] = (XORG_PROGRAM, PULSE_PROGRAM, SESSION_PROGRAM, NEKO_PROGRAM)

# The display cap (spec D1), the floor and the quantum of a requested size (spec section 7.2), and the modes
# xorg.conf is seeded with (spec section 7.1).
MAX_WIDTH: Final[int] = 1920
MAX_HEIGHT: Final[int] = 1080
MIN_WIDTH: Final[int] = 640
MIN_HEIGHT: Final[int] = 480
SIZE_QUANTUM_PX: Final[int] = 16
SEEDED_MODES: Final[tuple[tuple[int, int], ...]] = ((1920, 1080), (1600, 900), (1280, 720))

# The video ids of the three profiles (spec section 8.1), in neko's order.
VIDEO_ID_IDLE: Final[str] = "idle"
VIDEO_ID_WATCH: Final[str] = "watch"
VIDEO_ID_CONTROL: Final[str] = "control"
VIDEO_IDS: Final[tuple[str, ...]] = (VIDEO_ID_IDLE, VIDEO_ID_WATCH, VIDEO_ID_CONTROL)

# The profile chooser's clocks (spec section 8.1): how recent a human's input must be for ``control``, and how long
# with no input at all before ``idle``.
CONTROL_INPUT_WINDOW_SECONDS: Final[float] = 10.0
IDLE_AFTER_SECONDS: Final[float] = 60.0

# What the viewer's title bar reads for an empty desktop, and for a Chromium window whatever its page says
# (spec section 4.1).
DEFAULT_TITLE: Final[str] = "Screen"
BROWSER_TITLE: Final[str] = "Browser"

# The browser on the Screen (spec section 16): Fortress, where the env.d unit installs it (a stable path the unit
# keeps pointing at the pinned release), the pinned extensions beside it, the agent-facing CDP proxy, and where
# the one profile and the tab manifest live (the paths an upgraded workspace already has them at).
FORTRESS_EXECUTABLE: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")
FORTRESS_EXTENSIONS_DIR: Final[Path] = Path("/opt/fortress/extensions")
CDP_PROXY_HOST: Final[str] = "127.0.0.1"
CDP_PROXY_PORT: Final[int] = 8083
BROWSER_PROFILE_ROOT: Final[Path] = Path("/home/user/.mngr/browser-profiles")
BROWSER_PROFILE_DIR_PREFIX: Final[str] = "browser-use-user-data-dir-"
BROWSER_MANIFEST_PATH: Final[Path] = Path("data/.apps/browser/instances.json")
MAX_BROWSERS: Final[int] = 1
# A blank page has nothing to geolocate and nothing to consent to: an agent navigates the moment it attaches, and
# a human types an address.
BROWSER_HOME_URL: Final[str] = "about:blank"
# How Chromium's processes and windows are told apart from everything else on the Screen: the executable names a
# Fortress build runs under, and the markers its WM_CLASS carries.
CHROMIUM_EXECUTABLE_NAMES: Final[frozenset[str]] = frozenset({"chrome", "chromium", "chromium-browser", "tilion"})
CHROMIUM_WINDOW_CLASS_MARKERS: Final[tuple[str, ...]] = ("chrome", "chromium", "tilion", "fortress")

# A start page is an absolute http(s) URL with a host; the length bound keeps a query string from smuggling in an
# unbounded value.
MAX_START_URL_LENGTH: Final[int] = 2048
_START_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_URL_PREVIEW_LENGTH: Final[int] = 80
# The hosts of this machine, as a link opened in the workspace names them (``open:url``, the workspace link
# routing plan): the loopback names and any ``*.localhost`` host. ``urlsplit`` gives an IPv6 host without its
# brackets.
_LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCAL_HOSTNAME_SUFFIX: Final[str] = ".localhost"

# An X window id as wmctrl and xdotool print it: a hex literal.
_WINDOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^0x[0-9a-fA-F]{1,8}$")

# A dropped file keeps its name in the desktop folder (spec section 4.3), so the name must be one plain path
# segment: the filesystem's length limit, no separators, and no leading dot (a hidden file, ``.`` or ``..``).
MAX_DROPPED_FILE_NAME_BYTES: Final[int] = 255
_PATH_SEPARATORS: Final[frozenset[str]] = frozenset({"/", "\\", "\x00"})


class AgentId(NonEmptyStr):
    """The chat (or background agent) an agent-facing request speaks for: the ``X-Mngr-Agent-Id`` header."""


class NekoSessionId(NonEmptyStr):
    """A neko session id, as its login route and its events spell it."""


class NekoToken(NonEmptyStr):
    """A neko session or API token."""


class DisplayName(NonEmptyStr):
    """What neko shows a session as: the name the service derives from a request's identity."""


class XWindowId(str):
    """An X window id as the tools print it (``0x03400007``)."""

    def __new__(cls, value: str) -> Self:
        if not _WINDOW_ID_PATTERN.fullmatch(value):
            raise WindowToolError(f"invalid X window id {value!r}: expected a hex literal such as 0x03400007")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class RunCommand(tuple[str, ...]):
    """A command to start on the Screen: a non-empty argument vector of strings."""

    def __new__(cls, value: Any) -> Self:
        if isinstance(value, str) or not isinstance(value, (list, tuple)) or len(value) == 0:
            raise InvalidRunCommandError("a command is a non-empty list of strings")
        if not all(isinstance(argument, str) and argument != "" for argument in value):
            raise InvalidRunCommandError("every argument of a command is a non-empty string")
        return super().__new__(cls, tuple(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.any_schema())


class BrowserName(str):
    """A browser's name: lowercase alphanumeric words joined by single dashes, at most 40 characters, not all
    digits; safe as a URL path segment, a query value, and the profile directory's suffix."""

    def __new__(cls, value: str) -> Self:
        if not is_valid_browser_name(value):
            raise InvalidBrowserNameError(
                f"invalid browser name {value!r}: names are lowercase letters, digits, and single dashes, 1 to 40 "
                "characters, not all digits"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


@pure
def _preview(value: str) -> str:
    if len(value) <= _URL_PREVIEW_LENGTH:
        return repr(value)
    return repr(value[:_URL_PREVIEW_LENGTH] + "...")


@pure
def describe_start_url_problem(value: str) -> str | None:
    """Why ``value`` cannot be a start page, or None when it can."""
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
def describe_local_url_problem(value: str) -> str | None:
    """Why ``value`` cannot be a link the browser opens for the human, or None when it can."""
    problem = describe_start_url_problem(value)
    if problem is not None:
        return problem
    hostname = urllib.parse.urlsplit(value).hostname or ""
    if hostname not in _LOCAL_HOSTNAMES and not hostname.endswith(_LOCAL_HOSTNAME_SUFFIX):
        return f"invalid url {_preview(value)}: only an address on this machine (localhost) opens here"
    return None


@pure
def describe_dropped_file_name_problem(value: str) -> str | None:
    """Why ``value`` cannot name a dropped file in the desktop folder, or None when it can."""
    if value.strip() == "":
        return "a dropped file needs a name"
    if any(character in _PATH_SEPARATORS for character in value):
        return f"invalid file name {_preview(value)}: a name has no path separators"
    if value.startswith("."):
        return f"invalid file name {_preview(value)}: a name does not start with a dot"
    if any(character < " " or character == "\x7f" for character in value):
        return f"invalid file name {_preview(value)}: control characters are not allowed"
    if len(value.encode()) > MAX_DROPPED_FILE_NAME_BYTES:
        return f"invalid file name {_preview(value)}: over {MAX_DROPPED_FILE_NAME_BYTES} bytes"
    return None


class DroppedFileName(str):
    """The name a dropped file is saved under: one plain path segment, neither dot-prefixed nor over 255 bytes."""

    def __new__(cls, value: str) -> Self:
        problem = describe_dropped_file_name_problem(value)
        if problem is not None:
            raise InvalidDroppedFileNameError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class AbsoluteHttpUrl(str):
    """A browser's start page: an absolute http(s) URL with a host, at most 2048 characters, no whitespace or
    control characters."""

    def __new__(cls, value: str) -> Self:
        problem = describe_start_url_problem(value)
        if problem is not None:
            raise InvalidStartUrlError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class LocalHttpUrl(str):
    """A link the browser opens for the human: an absolute http(s) URL on a local host (``localhost``,
    ``127.0.0.1``, ``[::1]``, or a ``*.localhost`` host), held to a start page's rules."""

    def __new__(cls, value: str) -> Self:
        problem = describe_local_url_problem(value)
        if problem is not None:
            raise InvalidStartUrlError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())
