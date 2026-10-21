"""The shapes the daemon's JSON routes exchange, published in its API document (``app_schema``).

Every model here is what one route reads or answers; ``runner.ROUTE_TABLE`` names
which. The ownership answers mirror the dicts ``session.LiveBrowser`` builds on the
loop (``attach_for``, ``acquire_with_state``, ``handoff_with_state``), validated into
these models on the way out so the document and the daemon cannot disagree.
"""

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import ConfigDict, Field, RootModel

from browser.primitives import ControlOwner, Lifecycle


class FleetTab(FrozenModel):
    """One tab of a running browser, in the order the agent's tab commands index."""

    index: int = Field(description="The tab's position, the index tab commands take")
    title: str = Field(description="The page's title")
    url: str = Field(description="The page's URL")
    active: bool = Field(description="Whether this is the tab the browser shows")


class BrowserSummary(FrozenModel):
    """One browser of the fleet: its lifecycle, who controls it, and its tabs."""

    id: str = Field(
        description="The browser's name, which every other route addresses it by"
    )
    lifecycle: Lifecycle = Field(
        description="init while Chromium launches, running, crashed (gone for good), or stopped (kept, relaunchable)"
    )
    controller: ControlOwner = Field(
        description="Who controls the browser: the person, or an agent"
    )
    owner_agent_id: str | None = Field(
        description="The agent holding the browser, when an agent does"
    )
    owner_name: str | None = Field(
        description="That agent's display name, when it gave one"
    )
    human_pinned: bool = Field(
        description="Whether the person took control and keeps it until they hand back"
    )
    waiting: list[str] = Field(
        description="The agents queued for the browser, first in line first"
    )
    crashed: bool = Field(description="Whether Chromium died; such a browser is gone")
    tabs: list[FleetTab] = Field(
        description="The browser's tabs; empty unless it is running"
    )


class FleetListing(FrozenModel):
    """The whole fleet, and whether a new browser can be started right now."""

    browsers: list[BrowserSummary] = Field(description="Every browser of the fleet")
    can_create: bool = Field(description="Whether a create would be accepted now")
    create_reason: str = Field(
        description="Why a create would be refused; empty when it would not"
    )
    browser_count: int = Field(
        description="How many browsers count toward the fleet's cap"
    )
    browser_max: int = Field(description="The fleet's cap")


class CreateBrowserRequest(FrozenModel):
    """What ``POST /browsers`` takes: an optional name and an optional start page."""

    name: str | None = Field(
        default=None,
        description="The browser's name (lowercase letters, digits, single dashes); a free one is minted when omitted",
    )
    url: str | None = Field(
        default=None,
        description="An absolute http(s) URL to open first; the home page when omitted",
    )


class BrowserCreated(FrozenModel):
    """The browser a create answered; its Chromium may still be launching."""

    name: str = Field(description="The browser's name")


class LaunchRequest(FrozenModel):
    """What the shell posts to the ``new`` launch path; its envelope fields beside ``url`` are ignored."""

    model_config = ConfigDict(extra="ignore")

    url: str | None = Field(
        default=None,
        description="An absolute http(s) URL to open first; the home page when omitted",
    )


class LaunchedPage(FrozenModel):
    """Where the shell opens a window for the browser the launch answered."""

    path: str = Field(description="The browser's viewer page, as a path on this app")


class OpenUrlRequest(FrozenModel):
    """What the shell posts to the ``open:url`` handler; its envelope's ``type`` is ignored."""

    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(
        description="The shell client whose click asked, which the browser's window is shown to"
    )
    url: str = Field(description="An absolute http(s) URL on this machine")


class OpenedUrl(FrozenModel):
    """The browser that has the page, and the window the shell showed it in."""

    browser: str = Field(description="The browser the page opened in (or is held for)")
    window_id: str = Field(description="The window the shell showed")
    shown: str = Field(
        description="How the shell showed it, as its show op says (raised, opened, ...)"
    )


class PendingUrlCancelled(FrozenModel):
    """Whether a page the browser was holding for the person was dropped."""

    cancelled: bool = Field(
        description="True when a held page was dropped; False when none was held"
    )


class HealthStatus(FrozenModel):
    """Whether the daemon is up and whether it is still restoring its browsers."""

    status: str = Field(description="Always 'ok'")
    initializing: bool = Field(
        description="True until the saved fleet has been restored"
    )


class InitStatus(FrozenModel):
    """How far the daemon's startup restore has come."""

    phase: str = Field(description="initializing, waiting_for_chromium, or ready")
    reason: str | None = Field(
        default=None, description="Why Chromium is not ready yet, while waiting for it"
    )
    error: str | None = Field(
        default=None,
        description="Why the restore failed, when it did (the fleet is then empty)",
    )


class BrowserClosed(FrozenModel):
    """A browser was closed for good."""

    closed: bool = Field(description="Always true")


class BrowserStopped(FrozenModel):
    """A browser's Chromium was ended; the browser, its profile and its tabs are kept."""

    stopped: bool = Field(description="Always true")


class BrowserStarted(FrozenModel):
    """A stopped browser is relaunching on its saved tabs."""

    started: bool = Field(description="Always true")


class TabClosed(FrozenModel):
    """The tab the browser showed was closed; the browser stays up."""

    closed: bool = Field(description="Always true")


class BrowserReleased(FrozenModel):
    """Whether the calling agent gave the browser up."""

    released: bool = Field(
        description="True when the caller held the browser; False when it was not theirs"
    )


class ControlState(FrozenModel):
    """Who controls a browser, as every ownership answer reports it so an agent knows where it stands."""

    lifecycle: Lifecycle = Field(description="init, running, crashed, or stopped")
    controller: ControlOwner = Field(
        description="Who controls the browser: the person, or an agent"
    )
    owner_agent_id: str | None = Field(
        description="The agent holding the browser, when an agent does"
    )
    owner_name: str | None = Field(
        description="That agent's display name, when it gave one"
    )
    human_pinned: bool = Field(
        description="Whether the person took control and keeps it until they hand back"
    )


class AttachAnswer(ControlState):
    """The gated CDP URL the agent drives the browser at, or why it cannot have one yet."""

    ok: bool = Field(description="True when the URL was issued")
    attach_url: str | None = Field(
        default=None, description="The CDP URL to attach to, when issued"
    )
    status: str | None = Field(
        default=None,
        description="Why not, when not: busy_human, busy_agent, starting (retry shortly), crashed, or stopped",
    )
    hint: str | None = Field(
        default=None,
        description="How to bring a stopped browser back, when it is stopped",
    )


class AcquireRequest(FrozenModel):
    """What ``POST /browsers/{name}/acquire`` takes."""

    reclaim: bool = Field(
        default=False,
        description="Take the browser back from a person who took control, only when they said to",
    )


class AcquireAnswer(ControlState):
    """Whether the agent now holds the browser, or why not and whether it is queued."""

    ok: bool = Field(description="True when the agent holds the browser")
    status: str = Field(
        description="acquired, busy_human, busy_agent, starting (retry shortly), crashed, stopped, closed, or timed_out"
    )
    enqueued: bool = Field(
        description="Whether the agent was queued to be messaged when the browser frees"
    )


class HandoffRequest(FrozenModel):
    """What ``POST /browsers/{name}/handoff`` takes."""

    reason: str = Field(
        default="",
        description="What the person needs to do; 'human verification needed' when blank",
    )


class HandoffAnswer(ControlState):
    """Whether the browser was handed to the person, with the agent first in line to resume."""

    ok: bool = Field(description="True when the browser was handed over")
    status: str = Field(
        description="handed_off, or not_owner when the agent did not hold the browser"
    )


class ClientTelemetryRecord(FrozenModel):
    """One stripe's decode and paint timings as the viewer measured them; any field may be left out."""

    model_config = ConfigDict(extra="ignore")

    fid: float | None = Field(
        default=None, description="The frame the stripe belongs to"
    )
    y: float | None = Field(default=None, description="The stripe's row")
    t_arrived: float | None = Field(
        default=None, description="When the stripe arrived, in the viewer's clock"
    )
    t_decoded: float | None = Field(default=None, description="When it was decoded")
    t_painted: float | None = Field(default=None, description="When it was painted")
    dq: float | None = Field(
        default=None, description="The viewer's decode queue depth at the time"
    )
    err: bool | None = Field(
        default=None, description="True when the stripe failed to decode"
    )


class ClientTelemetryBatch(RootModel[list[ClientTelemetryRecord]]):
    """What the viewer posts: the records of a handful of stripes (a single record is accepted too)."""


class TelemetryAccepted(FrozenModel):
    """The viewer's records were taken in."""

    ok: bool = Field(description="Always true")


class ClipboardPasted(FrozenModel):
    """The viewer's clipboard bytes landed in the browser."""

    ok: bool = Field(description="Always true")
