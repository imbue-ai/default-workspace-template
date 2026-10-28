"""The request and answer models of the shell's routes that no other module owns (desktop contracts.md
sections 5 and 8): what the routes answer where the answer is not one of the layout library's records."""

from pydantic import Field
from pydantic import RootModel
from workspace_layout.ops import OpBody
from workspace_layout.primitives import ShowOutcome
from workspace_layout.records import DesktopView
from workspace_layout.records import WindowView

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.update_notice import UpdateNotice


class LayoutOpRequest(RootModel[OpBody]):
    """The body of the op route: one op's body, chosen by its ``op`` (desktop contracts.md section 8)."""


class AppRunningState(FrozenModel):
    """What an app's stop, start and quit answer: the app, and whether its program runs now."""

    name: str = Field(description="The app")
    is_running: bool = Field(description="Whether the app's program is running")


class DesktopDeletedResponse(FrozenModel):
    """What deleting a desktop answers."""

    fallback_desktop_id: str = Field(description="The desktop the deleted one's clients fall back to")


class WindowOpenedResponse(FrozenModel):
    """What opening a window answers: the window, whether it is new, and whether it waits for a desktop window."""

    window: WindowView = Field(description="The window opened, or the one already at the path")
    is_new: bool = Field(description="Whether the window was opened by this request (201) or found (200)")
    has_no_desktop_window: bool = Field(
        description="The client's only open windows are pop-outs, so the window shows when a desktop window opens"
    )


class LaunchResponse(FrozenModel):
    """What running a launch path answers: the window showing its page, the page's path, and whether it is new."""

    window: WindowView = Field(description="The window showing the launched page")
    path: str = Field(description="The page's path under the app's origin")
    is_new: bool = Field(description="Whether the window was opened by this request (201) or found (200)")


class PlacementsSavedResponse(FrozenModel):
    """What saving a client's placements answers: the stamp the next save must carry."""

    updated_at: str | None = Field(
        description="When the saved layout was written, as the read route spells it; null when nothing was saved"
    )


class ClientArrivalResponse(FrozenModel):
    """What a client's arrival answers: the desktop it lands on, and the desktop made or replaced for it."""

    desktop_id: str | None = Field(description="The desktop the client lands on; null when there is none yet")
    created_desktop: DesktopView | None = Field(description="The desktop made for the arrival, when one was")
    replaced_desktop_name: str | None = Field(description="The name of the desktop the arrival replaced, if any")


class MessageDeliveryView(FrozenModel):
    """What one app's handler did with a relayed message, as the relay route answers it."""

    app: str = Field(description="The app the message was delivered to")
    status: int | None = Field(
        description="The app's HTTP status for a posted message, 200 for a page shown, or null when the app could "
        "not be reached or the page not built or shown"
    )
    detail: str = Field(description="Empty when delivered; otherwise why the app did not take the message")
    shown: ShowOutcome | None = Field(description="For a page shown: which way the show went; null otherwise")
    has_no_desktop_window: bool | None = Field(
        description="For a page shown: whether the client's only open windows are pop-outs; null otherwise"
    )
    window_id: str | None = Field(description="For a page shown: the window showing it; null otherwise")


class EmbedderMessageRelayResponse(FrozenModel):
    """What relaying a message to the apps that handle its type answers: each app's delivery."""

    type: str = Field(description="The message type relayed")
    deliveries: tuple[MessageDeliveryView, ...] = Field(description="One per app registered for the type")
    detail: str | None = Field(
        description="Under 502: which apps did not take the message, and why; null when every app took it"
    )


class PendingUpdateResponse(FrozenModel):
    """The kept rollback point of the last careful-flow apply, or none."""

    notice: UpdateNotice | None = Field(description="The notice the windows show; null when there is none")


class UpdateRollbackStarted(FrozenModel):
    """What starting a rollback answers (202): the rollback is under way."""

    detail: str = Field(description="That the rollback has started")
