from enum import auto
from typing import Any

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from screen.primitives import AgentId
from screen.primitives import BrowserName
from screen.primitives import DisplayName
from screen.primitives import NekoSessionId
from screen.primitives import NekoToken
from screen.primitives import XWindowId
from screen.xorg_config import Modeline


class Lifecycle(UpperCaseStrEnum):
    """Where the stack is: the env.d unit still installing it, the group coming up, streaming, or ended."""

    INSTALLING = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPED = auto()


class Controller(UpperCaseStrEnum):
    """Who drives the Screen: a human session, an agent holding the lease, or nobody."""

    HUMAN = auto()
    AGENT = auto()
    NOBODY = auto()


class AcquireStatus(UpperCaseStrEnum):
    """What an agent's ``acquire`` came to (the fleet API's vocabulary, which its CLI's exit codes follow)."""

    ACQUIRED = auto()
    BUSY_HUMAN = auto()
    BUSY_AGENT = auto()
    TIMED_OUT = auto()


class TransportProtocol(UpperCaseStrEnum):
    """An ICE candidate's transport: what neko's UDP mux and TCP mux each carry (spec section 5.1)."""

    UDP = auto()
    TCP = auto()


class MediaCandidate(FrozenModel):
    """Where a published port of the workspace is reachable from a viewer: one rewritten ICE candidate's address."""

    host: str = Field(description="The address a viewer dials: an IP or hostname")
    port: int = Field(description="The port at that address")
    protocol: TransportProtocol = Field(description="The transport the address carries")
    guest_port: int | None = Field(
        default=None, description="The port inside the workspace the address forwards to, when the broker says"
    )


class IceServer(FrozenModel):
    """One ``RTCIceServer`` as a broker answered it, passed to the viewer unchanged (the TURN spec, section 4.2)."""

    # Cloudflare decides the entry's keys; whatever it adds rides along to the viewer.
    model_config = ConfigDict(extra="allow")

    urls: str | tuple[str, ...] = Field(description="The STUN or TURN URLs")
    username: str | None = Field(default=None, description="The TURN username, when the entry is a relay")
    credential: str | None = Field(default=None, description="The TURN credential, when the entry is a relay")


class IceServersAnswer(FrozenModel):
    """What a broker's ICE servers route answered: the servers and when the credential expires."""

    ice_servers: tuple[IceServer, ...] = Field(description="The servers, possibly none")
    expires_at: str | None = Field(description="RFC 3339 expiry of the credential, when there is one")


class ConnectInfo(FrozenModel):
    """The ``GET /api/connect-info`` answer (spec section 5.2): the union of what the brokers said in time."""

    ice_servers: tuple[IceServer, ...] = Field(description="Every ICE server the brokers answered")
    candidates: tuple[MediaCandidate, ...] = Field(description="Where the ICE mux port is reachable, de-duplicated")
    expires_at: str | None = Field(description="The earliest expiry among the credentials, when any")

    def document(self) -> dict[str, Any]:
        return {
            "ice_servers": [server.model_dump(exclude_none=True) for server in self.ice_servers],
            "candidates": [
                {"host": candidate.host, "port": candidate.port, "protocol": candidate.protocol.value.lower()}
                for candidate in self.candidates
            ],
            "expires_at": self.expires_at,
        }


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for in ``X-Imbue-Identity``: the owner flag always, the account when shared."""

    # The header is cross-version wire data from the proxies; a field this build does not know must never make
    # it unreadable.
    model_config = ConfigDict(extra="ignore")

    owner: bool = Field(description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(default=None, description="The account's user id (absent for an unshared workspace)")
    email: str | None = Field(
        default=None, description="The account's verified email, present exactly when user_id is"
    )
    name: str | None = Field(
        default=None, description="A display name an older proxy stamps; absent from current ones"
    )


class NekoSecrets(FrozenModel):
    """What neko is configured with at a stack start and what the service keeps to speak to it."""

    user_password: SecretStr = Field(description="The multiuser provider's password for human sessions")
    admin_password: SecretStr = Field(description="The multiuser provider's password for the service's event session")
    api_token: SecretStr = Field(description="The API token the service's REST calls carry")


class ScreenSize(FrozenModel):
    """A display mode as neko reports it."""

    width: int = Field(description="Width in pixels")
    height: int = Field(description="Height in pixels")
    rate: int = Field(description="Refresh rate in Hz")


class AddedMode(FrozenModel):
    """A RANDR mode the service added this stack life (spec section 7.2)."""

    name: str = Field(description="The mode's name, ``<width>x<height>_60``")
    width: int = Field(description="Width in pixels")
    height: int = Field(description="Height in pixels")


class DisplayModes(FrozenModel):
    """What ``xrandr --query`` reported: the connected output and the mode names it advertises."""

    output: str | None = Field(description="The connected output the modes belong to, or None when there is none")
    mode_names: tuple[str, ...] = Field(description="Every mode name listed under that output")


class ModeAddition(FrozenModel):
    """A mode the server must learn before neko can select it."""

    output: str = Field(description="The output to add the mode to")
    modeline: Modeline = Field(description="The CVT modeline to create")


class ModesRecord(FrozenModel):
    """``modes.json``: every mode added since the stack started."""

    model_config = ConfigDict(extra="ignore")

    modes: tuple[AddedMode, ...] = Field(default=(), description="The added modes, in order")


class QueuedAgent(FrozenModel):
    """An agent waiting to resume once the Screen frees."""

    agent_id: AgentId = Field(description="The agent's chat id")
    agent_name: str | None = Field(default=None, description="The agent's display name, when it gave one")


class ControlRecord(FrozenModel):
    """The lease as ``control.json`` keeps it across a service restart."""

    model_config = ConfigDict(extra="ignore")

    controller: Controller = Field(default=Controller.NOBODY, description="Who drives the Screen")
    owner_agent_id: AgentId | None = Field(default=None, description="The agent holding the lease, when one does")
    owner_agent_name: str | None = Field(default=None, description="That agent's display name")
    human_pinned: bool = Field(default=False, description="Whether the human took control explicitly")
    human_session_id: NekoSessionId | None = Field(
        default=None, description="The neko session of the human in control, when one is"
    )
    human_name: str | None = Field(default=None, description="That human's display name")
    resume_queue: tuple[QueuedAgent, ...] = Field(default=(), description="Agents to wake when the Screen frees")


class ControlView(FrozenModel):
    """The lease as the viewer and the agent command see it."""

    controller: Controller = Field(description="Who drives the Screen")
    human_pinned: bool = Field(description="Whether the human took control explicitly")
    human_session_id: NekoSessionId | None = Field(description="The neko session of the human in control")
    human_name: str | None = Field(description="That human's display name")
    agent_id: AgentId | None = Field(description="The agent holding the lease")
    agent_name: str | None = Field(description="That agent's display name")
    waiting: tuple[str, ...] = Field(description="Display names of the agents queued, in order")
    idle_seconds: int | None = Field(description="How long the agent's lease has been idle, while an agent holds it")
    idle_release_seconds: int | None = Field(description="Seconds until the idle lease frees itself")


class BrowserLifecycle(UpperCaseStrEnum):
    """Where a browser's Chromium is: registered and launching, up, dead unexpectedly, or stopped on purpose (by the
    user, or with the stack it ran on) with its profile and tabs kept."""

    INIT = auto()
    RUNNING = auto()
    CRASHED = auto()
    STOPPED = auto()


class ChromiumView(FrozenModel):
    """The browser as the viewer sees it (spec sections 4.2 and 16): the one browser's lifecycle, its name, and a
    page the human asked to open that is held while an agent drives."""

    state: str = Field(description="``absent`` while the fleet holds no browser, else the lifecycle, lower-case")
    name: BrowserName | None = Field(default=None, description="The browser's name, when there is one")
    pending_url: str | None = Field(default=None, description="The page held for the human, when one is")


class ScreenState(FrozenModel):
    """The ``GET /api/state`` document (docs/system/specs/screen/spec.md section 10.1)."""

    lifecycle: Lifecycle = Field(description="Where the stack is")
    install_reason: str | None = Field(description="What the env.d unit has not installed yet, while installing")
    stop_reason: str | None = Field(description="Why the stack ended, while stopped")
    title: str = Field(description="The frontmost X window's title, or the app's name for an empty desktop")
    video_id: str = Field(description="The video profile every viewer selects")
    control: ControlView = Field(description="The lease")
    screen_size: ScreenSize | None = Field(description="The display's current mode, while running")
    chromium: ChromiumView = Field(description="The browser's state")


class XWindow(FrozenModel):
    """One X window as ``wmctrl -lpxG`` lists it."""

    id: XWindowId = Field(description="The window id")
    desktop: int = Field(description="The desktop the window is on (-1 for a sticky window)")
    pid: int = Field(description="The owning process, or 0 when the window does not say")
    x: int = Field(description="Left edge")
    y: int = Field(description="Top edge")
    width: int = Field(description="Width in pixels")
    height: int = Field(description="Height in pixels")
    window_class: str = Field(description="The WM_CLASS, ``instance.class``")
    title: str = Field(description="The window's title")


class NekoSession(FrozenModel):
    """A neko session the service logged a human in to, or its own admin session."""

    session_id: NekoSessionId = Field(description="neko's session id")
    token: NekoToken = Field(description="The session token")
    name: DisplayName = Field(description="The display name the session was logged in with")
    is_owner: bool = Field(description="Whether the identity behind it is the workspace's owner")
    is_admin: bool = Field(description="Whether it was logged in with the admin password (the service's own)")


class ScreenStopped(FrozenModel):
    """What the watcher reports when the group ends: the member that left and why."""

    member: str = Field(description="The program that left RUNNING")
    reason: str = Field(description="How it left, in words")


class NekoSessionRecord(FrozenModel):
    """A session as neko's sessions route lists it: who it is and whether it is connected right now."""

    model_config = ConfigDict(extra="ignore")

    session_id: NekoSessionId = Field(description="neko's session id")
    name: str = Field(description="The session's display name")
    is_admin: bool = Field(description="Whether the session is an admin")
    is_connected: bool = Field(description="Whether a WebSocket is open for it")
    not_connected_since: str | None = Field(description="When it last disconnected, as neko timestamps it")
