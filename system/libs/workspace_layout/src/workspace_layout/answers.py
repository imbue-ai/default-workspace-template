from typing import Any
from typing import Final
from typing import TypeVar

from app_manifest.manifest import describe_validation_error
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from workspace_layout.errors import ShellAnswerMalformedError
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import WindowId

# The shell's answers are its to add to, so every answer model ignores a field it does not know.
_ANSWER_CONFIG: Final[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

# How much of an answer an error quotes.
ANSWER_QUOTE_LIMIT: Final[int] = 200


class DesktopOpAnswer(FrozenModel):
    """What the shell answers a document op with: the desktop it edited and the client whose placements it wrote."""

    model_config = _ANSWER_CONFIG

    desktop_id: DesktopId = Field(description="The desktop the op edited")
    client_id: ClientId | None = Field(description="The client the op targeted; None for an unplaced open")
    window_id: WindowId | None = Field(description="The window the op acted on, when it names one")
    # What the answer says about the client's popped-out windows (plan-popped-out-layout-ops.md); a shell older than
    # the rules answers none of it.
    is_raised_in_own_window: bool = Field(
        default=False, description="The window is popped out, so its own window was raised and it stayed out"
    )
    is_brought_back: bool = Field(
        default=False,
        description="The op brought a pulled-out window back onto the desktop (forced, or for a client that is not "
        "connected)",
    )
    unpaired_beside: WindowId | None = Field(
        default=None, description="The popped-out window an open's ``beside`` named, which the open did not pair with"
    )
    has_no_desktop_window: bool = Field(
        default=False,
        description="The client's only open windows are pop-outs, so the window the op put on the desktop shows when "
        "a desktop window opens",
    )


class OpenAnswer(DesktopOpAnswer):
    """What the shell answers an ``open`` with: the window it opened, or the one already at the path it focused."""

    window_id: WindowId = Field(description="The window opened or focused")


class ShowAnswer(OpenAnswer):
    """What the shell answers a ``show`` with: how it put the path on screen, and the window it used."""

    shown: str = Field(description="How the shell showed the path, as it spells it: raised, navigated, pinned, opened")


class ConnectedClient(FrozenModel):
    """One client of the shell's client list that holds a socket right now."""

    model_config = _ANSWER_CONFIG

    id: ClientId = Field(description="The client's id")
    active_desktop: DesktopId | None = Field(description="The desktop the client is on; None before it arrived")
    is_connected: bool = Field(description="Whether any window of the client holds the socket")


class DesktopSummary(FrozenModel):
    """One desktop of the shell's desktops document, in the shell's order (the first is the fallback desktop)."""

    model_config = _ANSWER_CONFIG

    id: DesktopId = Field(description="The desktop's id")
    name: str = Field(description="The desktop's name")


_Answer = TypeVar("_Answer", bound=FrozenModel)


@pure
def quote_answer(body: Any) -> str:
    """An answer as an error message quotes it: its ``detail`` when it has one, else the whole of it, shortened."""
    quoted = body.get("detail", body) if isinstance(body, dict) else body
    return str(quoted).strip()[:ANSWER_QUOTE_LIMIT]


@pure
def parse_answer(model: type[_Answer], body: Any, described: str) -> _Answer:
    """The answer ``body`` as ``model``; raises ShellAnswerMalformedError when it is not one."""
    if not isinstance(body, dict):
        raise ShellAnswerMalformedError(
            f"The shell answered the {described} with something else: {quote_answer(body)}"
        )
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise ShellAnswerMalformedError(
            f"The shell answered the {described} with something else ({describe_validation_error(e)}): "
            f"{quote_answer(body)}"
        ) from e


def parse_listing(model: type[_Answer], body: Any, key: str, described: str) -> list[_Answer]:
    """The entries of the list under ``key`` in ``body``, as ``model``.

    An entry that is not one is skipped with a warning, so one odd entry does not hide the rest; a body with no such
    list, or a non-empty list none of whose entries parse, raises ShellAnswerMalformedError, so a broken answer never
    reads as an empty one.
    """
    entries = body.get(key) if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise ShellAnswerMalformedError(
            f"The shell answered the {described} without a {key!r} list: {quote_answer(body)}"
        )
    parsed: list[_Answer] = []
    for entry in entries:
        try:
            parsed.append(model.model_validate(entry))
        except ValidationError as e:
            logger.warning(
                "Skipped an entry of the shell's {} that is not one: {}", described, describe_validation_error(e)
            )
    if entries and not parsed:
        raise ShellAnswerMalformedError(f"None of the {len(entries)} entries of the shell's {described} could be read")
    return parsed
