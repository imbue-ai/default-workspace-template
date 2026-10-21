"""What the shell's own routes answer: the health probe, who is here, and a heartbeat's identity."""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.identity import RequestIdentity


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


class PresenceListing(FrozenModel):
    """The connected users right now, one entry per user."""

    users: tuple[PresentUserView, ...] = Field(description="Every connected user")


class HeartbeatResponse(FrozenModel):
    """What a shell page's heartbeat answers: the requester's own identity record."""

    identity: RequestIdentity = Field(description="The requester the proxy vouched for")
