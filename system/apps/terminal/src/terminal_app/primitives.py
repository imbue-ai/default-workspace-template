import re
import urllib.parse
from collections.abc import Set as AbstractSet
from typing import Any, Final, Self

from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from terminal_app.errors import InvalidTerminalValueError

# A tmux session name is the terminal's name, and a query value in the wrapper's URL, so it is
# drawn from a URL-safe alphabet minus the two characters tmux itself refuses in a session name,
# "." and ":" (they separate the session, window, and pane parts of a tmux target).
MAX_SESSION_NAME_LENGTH: Final[int] = 128
TMUX_SESSION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_SESSION_NAME_LENGTH - 1}}}$"
)

MAX_WORKDIR_LENGTH: Final[int] = 1024

# A terminal's title is what its window is called: trimmed, non-blank, held to the length the
# shell keeps a window title to.
MAX_TERMINAL_TITLE_LENGTH: Final[int] = 256

# tmux's session id (``$3``): what a record is matched to a live session by (together with the
# session's creation time, since a later server hands the same ids out again), so a session
# renamed inside tmux keeps its name and title.
TMUX_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\$[0-9]+$")

# The memory-shedding band a terminal's shell (and everything run in it) is tagged into: a key
# of ``oom_priority.bands.SERVICE_BANDS``, passed to ``oom_tag_service.py`` by the session
# command. The pane would otherwise inherit the tmux server's fully protected 0.
TERMINAL_SESSION_BAND_KEY: Final[str] = "terminal-session"

# The names the app allocates, ``terminal-<N>``, whose titles derive back from the number ("Terminal 3").
TERMINAL_NAME_PREFIX: Final[str] = "terminal"
_NUMBERED_TERMINAL_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^{TERMINAL_NAME_PREFIX}-([0-9]+)$")
_ALLOCATED_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^{TERMINAL_NAME_PREFIX}-(?P<number>[1-9][0-9]*)$")

# The ttyd URL arguments, in the order the dispatch reads them: ``_`` lands in ``$0`` of the
# ``bash -c`` dispatch snippet, ``session`` selects the dispatch script, then the script's own
# positional arguments follow (session name, optional working directory).
_URL_ARGUMENT_KEY: Final[str] = "arg"
_URL_ARGUMENT_PLACEHOLDER: Final[str] = "_"
SESSION_DISPATCH_KEY: Final[str] = "session"

# The wrapper page's query parameter: which session it frames.
SESSION_QUERY_KEY: Final[str] = "session"


@pure
def _has_control_characters(value: str) -> bool:
    return any(character < " " or character == "\x7f" for character in value)


class TmuxSessionName(str):
    """A tmux session name, which is the terminal's name: URL-safe, without ``.`` and ``:``."""

    def __new__(cls, value: str) -> Self:
        if len(value) > MAX_SESSION_NAME_LENGTH:
            raise InvalidTerminalValueError(
                f"invalid session name: {len(value)} characters is over the {MAX_SESSION_NAME_LENGTH}-character limit"
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


class TmuxSessionId(str):
    """tmux's immutable id for a session, ``$<number>``, as ``#{session_id}`` prints it."""

    def __new__(cls, value: str) -> Self:
        if not TMUX_SESSION_ID_PATTERN.fullmatch(value):
            raise InvalidTerminalValueError(
                f"invalid session id {value!r}: ids match {TMUX_SESSION_ID_PATTERN.pattern}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class TerminalTitle(str):
    """What users see for a terminal: non-blank, whitespace-trimmed, at most 256 characters."""

    def __new__(cls, value: str) -> Self:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidTerminalValueError("invalid title: must not be blank")
        if len(trimmed) > MAX_TERMINAL_TITLE_LENGTH:
            raise InvalidTerminalValueError(
                f"invalid title: {len(trimmed)} characters is over the {MAX_TERMINAL_TITLE_LENGTH}-character limit"
            )
        return super().__new__(cls, trimmed)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class Workdir(str):
    """The directory a new terminal's shell starts in, as the ``new`` launch path's ``workdir`` parameter names it."""

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
def allocate_terminal_name(taken_names: AbstractSet[str]) -> TmuxSessionName:
    """The lowest free ``terminal-<N>`` (from 1), filling any gap a deletion left."""
    taken_numbers = {
        int(match.group("number"))
        for match in (_ALLOCATED_NAME_PATTERN.fullmatch(name) for name in taken_names)
        if match is not None
    }
    number = 1
    while number in taken_numbers:
        number += 1
    return TmuxSessionName(f"{TERMINAL_NAME_PREFIX}-{number}")


@pure
def derive_terminal_title(name: TmuxSessionName) -> TerminalTitle:
    """The title a session wears when nobody named it: ``Terminal 3`` for ``terminal-3``, any other name verbatim."""
    match = _NUMBERED_TERMINAL_PATTERN.fullmatch(name)
    if match is None:
        return TerminalTitle(name)
    return TerminalTitle(f"Terminal {match.group(1)}")


@pure
def session_page_path(name: TmuxSessionName) -> str:
    """The wrapper page for ``name``, the path a window of the terminal shows and reports."""
    return f"/?{SESSION_QUERY_KEY}={name}"


@pure
def pty_path_for_session(name: TmuxSessionName, workdir: Workdir | None) -> str:
    """The path on the pty origin that attaches to ``name``: the ttyd argument shape the dispatch reads."""
    arguments = [_URL_ARGUMENT_PLACEHOLDER, SESSION_DISPATCH_KEY, name]
    if workdir is not None:
        arguments.append(workdir)
    query = "&".join(
        f"{_URL_ARGUMENT_KEY}={urllib.parse.quote(argument, safe='')}"
        for argument in arguments
    )
    return f"/?{query}"
