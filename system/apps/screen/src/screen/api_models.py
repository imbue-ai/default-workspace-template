"""What the service's JSON routes take and answer (docs/system/specs/screen/spec.md section 10.1), the models the
route table publishes in the OpenAPI document at ``/api/schema`` and checks every parse and answer against
(``system/libs/app_schema``). A request model that the shell posts beside its own envelope fields, or that a script
may pad, ignores what it does not know; an answer is exactly its fields."""

from typing import Any

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SerializerFunctionWrapHandler
from pydantic import model_serializer

from imbue.imbue_common.frozen_model import FrozenModel
from screen.data_types import XWindow
from screen.primitives import RunCommand


class RequestModel(FrozenModel):
    """A request body as a route reads it: the fields it knows, anything else ignored (the shell's launch envelope,
    a script's extra keys)."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class HealthAnswer(FrozenModel):
    """``GET /health``: the service is up, and where the stack is."""

    status: str = Field(description="``ok`` while the service answers")
    lifecycle: str = Field(description="The stack's lifecycle, lower-case")


class ControlDocument(FrozenModel):
    """The lease as every control answer and the state document carry it (spec section 6.1)."""

    controller: str = Field(description="``human``, ``agent`` or ``nobody``")
    human_pinned: bool = Field(description="Whether the human took control explicitly (or was handed the Screen)")
    human_session_id: str | None = Field(description="The neko session of the human in control, when one is")
    human_name: str | None = Field(description="That human's display name")
    agent_id: str | None = Field(description="The agent holding the lease")
    agent_name: str | None = Field(description="That agent's display name")
    waiting: list[str] = Field(description="Display names of the agents queued, in order")
    idle_seconds: int | None = Field(description="How long the agent's lease has been idle, while an agent holds it")
    idle_release_seconds: int | None = Field(description="Seconds until the idle lease frees itself")


class ScreenSizeDocument(FrozenModel):
    """A display mode as neko reports it."""

    width: int = Field(description="Width in pixels")
    height: int = Field(description="Height in pixels")
    rate: int = Field(description="Refresh rate in Hz")


class ChromiumDocument(FrozenModel):
    """The browser as the viewer sees it (spec section 16)."""

    state: str = Field(description="``absent`` while the fleet holds no browser, else the lifecycle, lower-case")
    name: str | None = Field(description="The browser's name, when there is one")
    pending_url: str | None = Field(description="The page held for the human while an agent drives, when one is")


class StateDocument(FrozenModel):
    """``GET /api/state``: the lifecycle, the lease, the frontmost window's title, the video profile, the Screen's
    size and the browser's state."""

    lifecycle: str = Field(description="``installing``, ``starting``, ``running`` or ``stopped``")
    install_reason: str | None = Field(description="What the env.d unit has not installed yet, while installing")
    stop_reason: str | None = Field(description="Why the stack ended, while stopped")
    title: str = Field(description="The frontmost X window's title, or the app's name for an empty desktop")
    video_id: str = Field(description="The video profile every viewer selects")
    control: ControlDocument = Field(description="The lease")
    screen_size: ScreenSizeDocument | None = Field(description="The display's current mode, while running")
    chromium: ChromiumDocument = Field(description="The browser's state")


class StartAnswer(FrozenModel):
    """``POST /api/start``: the lifecycle after the start was asked for."""

    lifecycle: str = Field(description="The stack's lifecycle, lower-case")


class IceServerDocument(FrozenModel):
    """One ``RTCIceServer`` as a broker answered it, passed to the viewer unchanged: a STUN entry carries no
    credential fields at all, and whatever else the broker added rides along."""

    model_config = ConfigDict(extra="allow", frozen=True)

    urls: str | list[str] = Field(description="The STUN or TURN URLs")
    username: str | None = Field(default=None, description="The TURN username, when the entry is a relay")
    credential: str | None = Field(default=None, description="The TURN credential, when the entry is a relay")

    @model_serializer(mode="wrap")
    def _without_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}


class MediaCandidateDocument(FrozenModel):
    """Where the Screen's ICE mux port is reachable from this viewer."""

    host: str = Field(description="The address a viewer dials")
    port: int = Field(description="The port at that address")
    protocol: str = Field(description="``udp`` or ``tcp``")


class ConnectInfoAnswer(FrozenModel):
    """``GET /api/connect-info`` (spec section 5.2): the union of what the brokers answered in time."""

    ice_servers: list[IceServerDocument] = Field(description="Every ICE server the brokers answered, as given")
    candidates: list[MediaCandidateDocument] = Field(description="Where the ICE mux port is reachable")
    expires_at: str | None = Field(description="The earliest expiry among the relay credentials, when any")


class SessionAnswer(FrozenModel):
    """``POST /api/session``: a neko session for the requester's identity (spec section 6.3)."""

    session_id: str = Field(description="neko's session id")
    token: str = Field(description="The session token the viewer uses under ``/neko/``")
    name: str = Field(description="The display name the session was logged in with")
    is_owner: bool = Field(description="Whether the identity behind it is the workspace's owner")


class SessionRequest(RequestModel):
    """A viewer's verb for its session: ``session_id`` in the JSON body (or, for a script, the query string)."""

    session_id: str | None = Field(default=None, description="The neko session the request speaks for")


class ControlAnswer(ControlDocument):
    """A human verb's answer: whether it went through, and the lease after it."""

    ok: bool = Field(description="Whether the verb changed the lease")


class AcquireRequest(RequestModel):
    """``POST /api/control/acquire``: an agent's acquire, with ``reclaim`` to take the Screen from a human who told
    the agent to continue."""

    reclaim: bool = Field(default=False, description="Take the lease back from a human, on the user's instruction")


class AcquireAnswer(ControlDocument):
    """An agent's acquire: whether it holds the lease, the outcome, whether it was queued, and the lease."""

    ok: bool = Field(description="Whether the agent now holds the lease")
    status: str = Field(description="``acquired``, ``busy_human``, ``busy_agent``, ``timed_out`` or ``starting``")
    enqueued: bool = Field(description="Whether a refused agent was queued to resume when the Screen frees")


class ReleaseAnswer(ControlDocument):
    """An agent's release: whether it held the lease, and the lease after it."""

    released: bool = Field(description="Whether the agent held the lease it released")


class HandoffRequest(RequestModel):
    """``POST /api/control/handoff``: why the human is needed."""

    reason: str = Field(default="", description="What only the human can do; shown in the viewer")


class HandoffAnswer(ControlDocument):
    """An agent's handoff: whether it went through, and the lease after it."""

    ok: bool = Field(description="Whether the agent held the lease it handed off")
    status: str = Field(description="``handed_off`` or ``not_owner``")


class ScreenSizeRequest(RequestModel):
    """``POST /api/screen-size``: the controller's stage size in CSS pixels (spec section 7.2)."""

    width: int = Field(gt=0, description="The stage's width in CSS pixels")
    height: int = Field(gt=0, description="The stage's height in CSS pixels")
    session_id: str | None = Field(
        default=None, description="The viewer's session; absent for the ``screen size`` command, the controller's"
    )


class ScreenSizeAnswer(FrozenModel):
    """Whether the size was applied, why not, and the mode that stands."""

    applied: bool = Field(description="Whether the display was resized (or already had the size)")
    reason: str | None = Field(description="Why a report was not applied (``not the controller``)")
    screen_size: ScreenSizeDocument | None = Field(description="The mode that stands, while running")


class DropAnswer(FrozenModel):
    """``POST /api/drop``: where the files landed and under what names."""

    directory: str = Field(description="The Screen's desktop folder")
    saved: list[str] = Field(description="The name each file was saved under, in the order sent")


class WindowsAnswer(FrozenModel):
    """``GET /api/windows``: the X windows on the display."""

    windows: list[XWindow] = Field(description="Every window ``wmctrl`` lists")


class ActivatedAnswer(FrozenModel):
    """``POST /api/windows/{window_id}/activate``: the window was raised."""

    activated: bool = Field(description="Always true on success")


class RunRequest(RequestModel):
    """``POST /api/run``: the command to start on the display."""

    command: RunCommand = Field(description="The argument vector, non-empty strings")


class RunAnswer(FrozenModel):
    """``POST /api/run``: the started process."""

    pid: int = Field(description="The detached process's pid")


class StoppedNotification(RequestModel):
    """The watcher's notification that a member of the stack left RUNNING."""

    member: str = Field(default="", description="The program that left RUNNING")
    reason: str = Field(default="", description="How it left, in words")


class LaunchBrowserRequest(RequestModel):
    """``POST /launch/browser``: the start page, if any, beside the shell's launch envelope."""

    url: str | None = Field(default=None, description="An absolute http(s) URL to open as a tab in front")


class LaunchAnswer(FrozenModel):
    """A launch path's answer: the page the shell opens."""

    path: str = Field(description="The Screen's one page, ``/``")


class OpenUrlRequest(RequestModel):
    """``POST /api/open-url``: the ``open:url`` message the shell relays."""

    url: str = Field(description="A link to an address on this machine")
    client_id: str = Field(description="The shell client that clicked the link")


class OpenUrlAnswer(FrozenModel):
    """``POST /api/open-url``: the browser that has the page, and the Screen window shown to the client."""

    browser: str = Field(description="The browser's name")
    window_id: str = Field(description="The Screen window the shell showed")
    shown: str = Field(description="How the shell put the Screen's page on screen")


class TabDocument(FrozenModel):
    """One of a running browser's tabs, in the order the agent's ``tab-select`` indexes."""

    index: int = Field(description="The tab's position")
    title: str = Field(description="The page's title")
    url: str = Field(description="The page's URL")
    active: bool = Field(description="Whether the tab is in front")


class BrowserDescription(FrozenModel):
    """One browser as ``GET /browsers`` lists it: the lifecycle, the lease as the fleet CLI reads it, the tabs."""

    id: str = Field(description="The browser's name")
    lifecycle: str = Field(description="``init``, ``running``, ``crashed`` or ``stopped``")
    controller: str = Field(description="``human``, ``agent`` or ``nobody``")
    owner_agent_id: str | None = Field(description="The agent holding the Screen's lease")
    owner_name: str | None = Field(description="That agent's display name")
    human_pinned: bool = Field(description="Whether a human holds the Screen explicitly")
    human_name: str | None = Field(description="The human controller's display name")
    waiting: list[str] = Field(description="Display names of the agents queued for the Screen")
    crashed: bool = Field(description="Whether Chromium died unexpectedly")
    pending_url: str | None = Field(description="The page held for the human while an agent drives, when one is")
    tabs: list[TabDocument] = Field(description="The tabs, while the browser runs")


class BrowsersAnswer(FrozenModel):
    """``GET /browsers``: the fleet and whether another browser can be created."""

    browsers: list[BrowserDescription] = Field(description="Every browser of the fleet")
    can_create: bool = Field(description="Whether Chromium is installed, so a browser can start")
    create_reason: str = Field(description="Why not, when it cannot; empty otherwise")
    browser_count: int = Field(description="How many browsers are launched")
    browser_max: int = Field(description="The fleet's cap")


class CreateBrowserRequest(RequestModel):
    """``POST /browsers``: a browser by name, or the one browser, with an optional start page."""

    name: str | None = Field(default=None, description="The browser's name; absent for the one browser")
    url: str | None = Field(default=None, description="An absolute http(s) URL to open as a tab in front")


class BrowserNameAnswer(FrozenModel):
    """``POST /browsers``: the browser the request means, answered while Chromium launches."""

    name: str = Field(description="The browser's name")


class ClosedAnswer(FrozenModel):
    """The browser, or its tab in front, was closed."""

    closed: bool = Field(description="Always true on success")


class StoppedAnswer(FrozenModel):
    """``POST /browsers/{raw_name}/stop``: Chromium was ended with the browser kept."""

    stopped: bool = Field(description="Always true on success")


class StartedAnswer(FrozenModel):
    """``POST /browsers/{raw_name}/start``: Chromium was started again on its saved tabs."""

    started: bool = Field(description="Always true on success")


class CancelledAnswer(FrozenModel):
    """``POST /browsers/{raw_name}/pending-url/cancel``: whether a held page was dropped."""

    cancelled: bool = Field(description="Whether a page was held")


class ReleasedAnswer(FrozenModel):
    """``POST /browsers/{raw_name}/release``: whether the agent held the lease it released."""

    released: bool = Field(description="Whether the agent held the lease")


class ControlPayload(FrozenModel):
    """The lease snapshot every fleet answer carries, so an agent sees whether it still holds the Screen."""

    lifecycle: str = Field(description="The browser's lifecycle, lower-case")
    controller: str = Field(description="``human``, ``agent`` or ``nobody``")
    owner_agent_id: str | None = Field(description="The agent holding the Screen's lease")
    owner_name: str | None = Field(description="That agent's display name")
    human_pinned: bool = Field(description="Whether a human holds the Screen explicitly")


class AttachAnswer(ControlPayload):
    """``GET /browsers/{raw_name}/attach``: the agent's attach URL, or why it cannot have one."""

    ok: bool = Field(description="Whether an attach URL was issued")
    status: str | None = Field(
        default=None, description="Why not: ``crashed``, ``stopped``, ``starting``, ``busy_human`` or ``busy_agent``"
    )
    hint: str | None = Field(default=None, description="How to start a stopped browser")
    attach_url: str | None = Field(default=None, description="The CDP proxy URL the agent drives through")


class BrowserAcquireAnswer(AcquireAnswer):
    """``POST /browsers/{raw_name}/acquire`` over a running browser: the acquire, and the browser's lifecycle."""

    lifecycle: str = Field(description="The browser's lifecycle, lower-case")
