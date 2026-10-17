import re
from typing import Any
from typing import Final
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from app_manifest.primitives import AppName
from imbue.imbue_common.primitives import NonEmptyStr
from screen.errors import InvalidRunCommandError
from screen.errors import WindowToolError

# The app's registered name (what its manifest, system/apps/screen/app.toml, declares and the
# supervisord program line registers).
APP_NAME: Final[AppName] = AppName("screen")

# The one display (docs/system/specs/screen/spec.md D16), taken from the browser's Xvfb.
DISPLAY: Final[str] = ":99"
DISPLAY_SOCKET_PATH: Final[str] = "/tmp/.X11-unix/X99"

# Where the service, neko and the ICE muxes listen (spec section 5.1).
SERVICE_HOST: Final[str] = "127.0.0.1"
SERVICE_PORT: Final[int] = 8090
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

# What the viewer's title bar reads for an empty desktop (spec section 4.1).
DEFAULT_TITLE: Final[str] = "Screen"

# An X window id as wmctrl and xdotool print it: a hex literal.
_WINDOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^0x[0-9a-fA-F]{1,8}$")


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
