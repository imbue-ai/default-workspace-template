"""What the shell's own routes answer: the health probe, who is here, and a heartbeat's identity."""

from typing import Annotated
from typing import Final

from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ParticipantKey

from imbue.imbue_common.frozen_model import FrozenModel

# The oldest input age a heartbeat may report: a page's age is at most its own lifetime, and the bound keeps the
# moment the shell stamps from it inside what a datetime can hold (an age of Infinity or 1e300 would overflow).
MAX_INPUT_AGE_SECONDS: Final[float] = 100 * 365 * 24 * 60 * 60


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
    """What a shell page's heartbeat may carry: how long ago the page last saw keyboard or pointer input (the
    multiplayer spec, section 4.3). A page from before the age was sent reports the moment by its own clock instead,
    which is taken no later than the shell's now; an older page still sends other fields, or none, which are
    ignored. An age beyond ``MAX_INPUT_AGE_SECONDS`` is refused as every malformed body is."""

    model_config = ConfigDict(extra="ignore")

    input_age_seconds: Annotated[float, Field(ge=0, le=MAX_INPUT_AGE_SECONDS)] | None = Field(
        default=None,
        description="How many seconds ago the page last saw keyboard or pointer input; absent when it has seen none",
    )
    last_input_at: AwareDatetime | None = Field(
        default=None,
        description="When the page last saw input by its own clock, as a page from before the age was sent reports it",
    )


class HeartbeatResponse(FrozenModel):
    """What a shell page's heartbeat answers: the requester's own identity record, and the participant key the page
    is known by (the multiplayer spec, section 4.1), which is how it tells its own entries from the rest."""

    identity: RequestIdentity = Field(description="The requester the proxy vouched for")
    participant: ParticipantKey = Field(description="The key the requester's clients, messages, and desktops carry")
