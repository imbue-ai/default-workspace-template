"""The shell's answers (desktop contracts.md sections 5 and 8): what the shell builds each answer as, and what a
caller parses it with.

The shell's answers are its to add to, so a caller parses one with unknown fields ignored (``parse_answer``,
``parse_listing``); the shell builds the same models strictly.
"""

from typing import Any
from typing import Literal
from typing import TypeVar

from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import MessageHandler
from app_manifest.manifest import Pin
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import RegistryLaunchPath
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from workspace_layout.errors import ShellAnswerMalformedError
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import WindowId
from workspace_layout.records import ClientRecord
from workspace_layout.records import DesktopLayoutView
from workspace_layout.records import DesktopView
from workspace_layout.transport import quote_answer


class DesktopOpAnswer(FrozenModel):
    """What the shell answers a document op with (desktop contracts.md section 8): the desktop it edited and the
    target client's layout of it."""

    ok: Literal[True] = Field(default=True, description="The op was applied")
    desktop_id: DesktopId = Field(description="The desktop the op edited")
    client_id: ClientId | None = Field(description="The client the op targeted; None for an unplaced open")
    desktop: DesktopView = Field(description="The desktop after the op")
    layout: DesktopLayoutView | None = Field(description="The client's layout of the desktop; None for no client")
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

    shown: ShowOutcome = Field(description="How the shell showed the path")


class TransientOpAnswer(FrozenModel):
    """What the shell answers an op its client's windows apply (``refresh``, ``reload_system_interface``) with."""

    ok: Literal[True] = Field(default=True, description="The op was sent")
    target_client_id: ClientId | None = Field(description="The client whose windows apply it; None for every client")


class ClientView(ClientRecord):
    """The ``client`` object of desktop contracts.md section 5.5: the record, and whether it is connected."""

    is_connected: bool = Field(description="Whether any window of the client holds the socket")


class PoppedOutWindow(FrozenModel):
    """A window a client's layout says is pulled out into its own window (desktop contracts.md section 4.2)."""

    window_id: WindowId = Field(description="The window")
    desktop_id: DesktopId = Field(description="The desktop it is on")
    is_ghost_hidden: bool = Field(description="Whether the user hid its ghost on the desktop (``is_minimized``)")


class InventoryClient(ClientView):
    """A client of the inventory document: the client, the windows of its active desktop it shows, and the windows
    it popped out of any desktop."""

    shown: tuple[WindowId, ...] = Field(description="The windows of its active desktop its layout does not minimize")
    popped_out: tuple[PoppedOutWindow, ...] = Field(
        default=(), description="The windows its layouts of every desktop say are popped out; none from an older shell"
    )


class InventoryApp(FrozenModel):
    """The ``app`` object of desktop contracts.md section 5.5: an app's registry row and whether it runs."""

    name: AppName = Field(description="The registered app name")
    display_name: str = Field(description="What users see; the name for a row without one")
    icon: str = Field(description="The registered SVG markup; empty for none")
    label: str = Field(description="The unguessable origin label")
    url: AppUrl = Field(description="Where the app is reachable from inside the workspace")
    internal: bool = Field(description="Hidden from every open surface")
    program: str = Field(description="The supervisord program that runs the app; empty for none")
    critical: bool = Field(description="Whether the workspace never stops the app")
    stop_when_no_windows: bool = Field(description="Whether the app is stopped once no window shows it")
    launch_paths: tuple[RegistryLaunchPath, ...] = Field(description="The launch paths the app offers")
    default_shortcut: DefaultShortcut | None = Field(description="The shortcut a new desktop is seeded with")
    launcher_rank: int | None = Field(description="Where the app sits in the launcher")
    pin: Pin | None = Field(description="The app's pinned taskbar entry")
    message_handlers: tuple[MessageHandler, ...] = Field(description="The message types the app handles")
    is_running: bool = Field(description="Derived from supervisord or a TCP probe, never stored")


class InventoryDocument(FrozenModel):
    """The one document of desktop contracts.md section 5.5 (``GET /api/inventory``)."""

    is_preview: bool = Field(description="Whether a preview shell answered")
    workspace_name: str = Field(description="The workspace's name")
    desktops: tuple[DesktopView, ...] = Field(description="Every desktop, in the shell's order")
    apps: tuple[InventoryApp, ...] = Field(description="Every registered app")
    clients: tuple[InventoryClient, ...] = Field(description="Every known client")


class InventoryOpAnswer(InventoryDocument):
    """What the shell answers the inventory ops (``desktops``, ``list``) with: the inventory document."""

    ok: Literal[True] = Field(default=True, description="The op was answered")


class DesktopsListing(FrozenModel):
    """``GET /api/desktops`` (desktop contracts.md section 5.2): every desktop, the first being the fallback."""

    desktops: tuple[DesktopView, ...] = Field(description="Every desktop, in creation order")


class ClientsListing(FrozenModel):
    """``GET /api/clients``: every known client, its desktop settled."""

    clients: tuple[ClientView, ...] = Field(description="Every known client")


class RecentClientMessage(FrozenModel):
    """One message a client sent to an app's page, as the ``context`` op lists it."""

    timestamp: str = Field(description="When the message was sent")
    app: str = Field(description="The app the message went to")
    key: str = Field(description="The marker of the page it went to (a chat id); empty for a page without one")
    text: str = Field(description="The message text, truncated at write time")


class ClientActivitySummary(FrozenModel):
    """One client as the ``context`` op lists it: where it is, whether it is connected, and what it sent lately."""

    client_id: ClientId = Field(description="The client")
    active_desktop: DesktopId | None = Field(description="The desktop it is on, as last reported")
    last_seen: str = Field(description="When it last logged anything; empty for a client that logged nothing")
    is_connected: bool = Field(description="Whether any window of the client holds the socket")
    recent_messages: tuple[RecentClientMessage, ...] = Field(description="Its latest messages, oldest first")


class ContextAnswer(FrozenModel):
    """What the shell answers the ``context`` op with: every client, most recently seen first."""

    ok: Literal[True] = Field(default=True, description="The op was answered")
    clients: tuple[ClientActivitySummary, ...] = Field(description="Every client, most recently seen first")


class LayoutOpMessageArgs(FrozenModel):
    """What a ``layout_op`` message tells the client's windows: the window or app it is about."""

    window: WindowId | None = Field(default=None, description="The window the op is about")
    app: AppName | None = Field(default=None, description="The app every page of which a whole-app refresh reloads")
    is_detached: bool | None = Field(
        default=None, description="For a ``show``: whether the window is pulled out into its own desktop window"
    )


class LayoutOpMessage(FrozenModel):
    """The ``layout_op`` WebSocket message (desktop contracts.md section 6): an op the client's windows apply."""

    type: Literal["layout_op"] = Field(default="layout_op", description="The message type")
    op: LayoutOp = Field(description="The op")
    args: LayoutOpMessageArgs = Field(description="What the op is about")
    requester: str = Field(description="The requester as ``<app>`` or ``<app>:<marker>``; empty for none")
    target_client_id: ClientId | None = Field(description="The client whose windows apply it; None for every window")


_Answer = TypeVar("_Answer", bound=FrozenModel)


@pure
def parse_answer(model: type[_Answer], body: Any, described: str) -> _Answer:
    """The answer ``body`` as ``model``; raises ShellAnswerMalformedError when it is not one."""
    if not isinstance(body, dict):
        raise ShellAnswerMalformedError(
            f"The shell answered the {described} with something else: {quote_answer(body)}"
        )
    try:
        return model.model_validate(body, extra="ignore")
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
            parsed.append(model.model_validate(entry, extra="ignore"))
        except ValidationError as e:
            logger.warning(
                "Skipped an entry of the shell's {} that is not one: {}", described, describe_validation_error(e)
            )
    if entries and not parsed:
        raise ShellAnswerMalformedError(f"None of the {len(entries)} entries of the shell's {described} could be read")
    return parsed
