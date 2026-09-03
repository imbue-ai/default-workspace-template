import re
import urllib.parse
from typing import Any, Final, Self

from app_instances.primitives import (
    MAX_INSTANCE_KEY_LENGTH,
    TAB_PLACEHOLDER,
    InstanceTitle,
    InstanceUrl,
)
from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from terminal_app.errors import InvalidTerminalValueError

# A tmux session name doubles as the instance key (contracts.md section 4.3), so it is drawn
# from the key alphabet minus the two characters tmux itself refuses in a session name, "." and
# ":" (they separate the session, window, and pane parts of a tmux target).
TMUX_SESSION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_INSTANCE_KEY_LENGTH - 1}}}$"
)

# The per-tab id the dispatch receives and records a pty under: today the frontend's
# ``term-<uuid>``, from phase 7 of the model the shell's ``tab-<hex>``. It names a file, so it is
# held to the key alphabet.
TERMINAL_TAB_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_INSTANCE_KEY_LENGTH - 1}}}$"
)

MAX_WORKDIR_LENGTH: Final[int] = 1024

# The names the allocator mints, whose titles derive back from the number ("Terminal 3").
_NUMBERED_TERMINAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^terminal-([0-9]+)$")

# The ttyd URL arguments, in the order the dispatch reads them: ``_`` lands in ``$0`` of the
# ``bash -c`` dispatch snippet, ``session`` selects the dispatch script, then the script's own
# positional arguments follow (session name, tab id, optional working directory).
_URL_ARGUMENT_KEY: Final[str] = "arg"
_URL_ARGUMENT_PLACEHOLDER: Final[str] = "_"
SESSION_DISPATCH_KEY: Final[str] = "session"


@pure
def _has_control_characters(value: str) -> bool:
    return any(character < " " or character == "\x7f" for character in value)


class TmuxSessionName(str):
    """A tmux session name that is also an instance key: the key alphabet without ``.`` and ``:``."""

    def __new__(cls, value: str) -> Self:
        if len(value) > MAX_INSTANCE_KEY_LENGTH:
            raise InvalidTerminalValueError(
                f"invalid session name: {len(value)} characters is over the {MAX_INSTANCE_KEY_LENGTH}-character limit"
            )
        if not TMUX_SESSION_NAME_PATTERN.fullmatch(value):
            raise InvalidTerminalValueError(
                f"invalid session name {value!r}: names match {TMUX_SESSION_NAME_PATTERN.pattern}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class TerminalTabId(str):
    """The id of the tab a ttyd client serves, as the dispatch received it in the URL."""

    def __new__(cls, value: str) -> Self:
        if not TERMINAL_TAB_ID_PATTERN.fullmatch(value):
            raise InvalidTerminalValueError(
                f"invalid tab id {value!r}: ids match {TERMINAL_TAB_ID_PATTERN.pattern}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class ClientTty(str):
    """The pty a tmux client is attached through (``/dev/pts/7``)."""

    def __new__(cls, value: str) -> Self:
        if (
            not value.startswith("/")
            or any(character.isspace() for character in value)
            or _has_control_characters(value)
        ):
            raise InvalidTerminalValueError(
                f"invalid client tty {value!r}: expected a device path such as /dev/pts/7"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class Workdir(str):
    """The directory a new terminal's shell starts in, as the ``new`` action's ``workdir`` parameter names it."""

    def __new__(cls, value: str) -> Self:
        if not value or _has_control_characters(value):
            raise InvalidTerminalValueError(
                f"invalid workdir {value!r}: must be a non-empty path without control characters"
            )
        if len(value) > MAX_WORKDIR_LENGTH:
            raise InvalidTerminalValueError(
                f"invalid workdir: {len(value)} characters is over the {MAX_WORKDIR_LENGTH}-character limit"
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
def derive_terminal_title(name: TmuxSessionName) -> InstanceTitle:
    """The title a session wears when nobody named it: ``Terminal 3`` for ``terminal-3``, any other name verbatim.

    The same rule the shell's frontend applies today (``derived-names.ts``), so no title moves
    when the shell starts reading titles from this app.
    """
    match = _NUMBERED_TERMINAL_PATTERN.fullmatch(name)
    if match is None:
        return InstanceTitle(name)
    return InstanceTitle(f"Terminal {match.group(1)}")


@pure
def instance_url_for_session(
    name: TmuxSessionName, workdir: Workdir | None
) -> InstanceUrl:
    """The URL a tab opens to attach to ``name``: today's ttyd argument shape, with the tab placeholder in the tab id slot."""
    arguments = [_URL_ARGUMENT_PLACEHOLDER, SESSION_DISPATCH_KEY, name, TAB_PLACEHOLDER]
    if workdir is not None:
        arguments.append(workdir)
    query = "&".join(
        f"{_URL_ARGUMENT_KEY}={urllib.parse.quote(argument, safe='{}')}"
        for argument in arguments
    )
    return InstanceUrl(f"/?{query}")
