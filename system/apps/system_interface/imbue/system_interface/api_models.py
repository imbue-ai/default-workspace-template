"""What the shell's own routes answer: the health probe, who is here, and a heartbeat's identity."""

from typing import Annotated
from typing import Any
from typing import Final
from typing import Self

from loguru import logger
from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ModelWrapValidatorHandler
from pydantic import ValidationError
from pydantic import model_validator
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import ParticipantKey

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.data_types import WindowInputReport

# The oldest input age a heartbeat may report: a page's age is at most its own lifetime, and the bound keeps the
# moment the shell stamps from it inside what a datetime can hold (an age of Infinity or 1e300 would overflow).
MAX_INPUT_AGE_SECONDS: Final[float] = 100 * 365 * 24 * 60 * 60
# The heartbeat fields that never refuse it: a value off its shape drops these, and only these, from the body.
_INPUT_REPORT_FIELDS: Final[frozenset[str]] = frozenset({"client_id", "input_windows"})


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
    multiplayer spec, section 4.3), and which windows a person interacted with since the last heartbeat (the
    app-usage-signals spec, section 6.3). A page from before the age was sent reports the moment by its own clock
    instead, which is taken no later than the shell's now; an older page still sends other fields, or none, which are
    ignored. An age beyond ``MAX_INPUT_AGE_SECONDS`` is refused as every malformed body is.

    The input-window fields alone never refuse the heartbeat: a refused heartbeat would record no presence, and a page
    keeps its reports across a failed send, so a body the shell could not read would take the person off the roster
    for good. A client id off its rule, or a window entry off its shape, drops both fields with a warning instead."""

    model_config = ConfigDict(extra="ignore")

    input_age_seconds: Annotated[float, Field(ge=0, le=MAX_INPUT_AGE_SECONDS)] | None = Field(
        default=None,
        description="How many seconds ago the page last saw keyboard or pointer input; absent when it has seen none",
    )
    last_input_at: AwareDatetime | None = Field(
        default=None,
        description="When the page last saw input by its own clock, as a page from before the age was sent reports it",
    )
    client_id: ClientId | None = Field(
        default=None, description="The page's own client id; absent from a page from before the input reports"
    )
    input_windows: tuple[WindowInputReport, ...] = Field(
        default=(), description="The windows whose pages reported input since the last answered heartbeat"
    )

    @model_validator(mode="wrap")
    @classmethod
    def _drop_input_reports_off_their_shape(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Validate the body as usual, and when every error lies in the input-window fields, validate it again
        without them; an error anywhere else (the age, the body's own shape) refuses the body as before."""
        try:
            return handler(data)
        except ValidationError as e:
            is_only_the_reports = isinstance(data, dict) and all(
                len(error["loc"]) > 0 and error["loc"][0] in _INPUT_REPORT_FIELDS for error in e.errors()
            )
            if not is_only_the_reports:
                raise
            logger.warning("Dropped a heartbeat's input reports, which were off their shape: {}", e)
            return handler({key: value for key, value in data.items() if key not in _INPUT_REPORT_FIELDS})


class HeartbeatResponse(FrozenModel):
    """What a shell page's heartbeat answers: the requester's own identity record, and the participant key the page
    is known by (the multiplayer spec, section 4.1), which is how it tells its own entries from the rest."""

    identity: RequestIdentity = Field(description="The requester the proxy vouched for")
    participant: ParticipantKey = Field(description="The key the requester's clients, messages, and desktops carry")
