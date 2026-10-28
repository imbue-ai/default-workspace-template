"""What the shell's own routes answer: the health probe, who is here, and a heartbeat's identity."""

from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ParticipantKey

from imbue.imbue_common.frozen_model import FrozenModel


class ShellHealthResponse(FrozenModel):
    """What the probe route answers (contracts.md section 5)."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the built frontend is being served")


class PresentUserView(FrozenModel):
    """One user who is here, with the account's profile."""

    user_id: str = Field(description="The account's user id")
    email: str = Field(description="The account's verified email as of its last heartbeat")
    display_name: str | None = Field(description="What the account calls itself, when its profile is known")
    profile_picture_url: str | None = Field(description="The account's picture, when its profile is known")
    owner: bool = Field(description="Whether this user owns the workspace")
    first_seen: str = Field(description="ISO timestamp of the heartbeat that created the record")
    last_seen: str = Field(description="ISO timestamp of the latest heartbeat")
    last_input_at: str | None = Field(
        description="ISO timestamp of the newest keyboard or pointer input a page of theirs reported, when any"
    )


class PresenceListing(FrozenModel):
    """The connected users right now, one entry per user."""

    users: tuple[PresentUserView, ...] = Field(description="Every connected user")


class HeartbeatRequest(FrozenModel):
    """What a shell page's heartbeat may carry: when the page last saw keyboard or pointer input. An older page
    sends other fields, or none, which are ignored."""

    model_config = ConfigDict(extra="ignore")

    last_input_at: AwareDatetime | None = Field(
        default=None, description="When the page last saw keyboard or pointer input; absent when it has seen none"
    )


class HeartbeatResponse(FrozenModel):
    """What a shell page's heartbeat answers: the requester's own identity record, and the participant key the page
    is known by (the multiplayer spec, section 4.1), which is how it tells its own entries from the rest."""

    identity: RequestIdentity = Field(description="The requester the proxy vouched for")
    participant: ParticipantKey = Field(description="The key the requester's clients, messages, and desktops carry")
