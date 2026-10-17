"""The profile chooser (docs/system/specs/screen/spec.md section 8.1): one video id for every viewer, from who holds
control and when input last arrived."""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.data_types import Controller
from screen.primitives import CONTROL_INPUT_WINDOW_SECONDS
from screen.primitives import IDLE_AFTER_SECONDS
from screen.primitives import VIDEO_ID_CONTROL
from screen.primitives import VIDEO_ID_IDLE
from screen.primitives import VIDEO_ID_WATCH


class ProfileInputs(FrozenModel):
    """The aggregate state the chooser reads."""

    controller: Controller = Field(description="Who holds the lease")
    seconds_since_input: float | None = Field(
        description="How long since a human's input relay or an agent's command last arrived; None for never"
    )


@pure
def choose_video_id(inputs: ProfileInputs) -> str:
    """``control`` while a human holds control and sent input in the last ten seconds; ``idle`` while nobody has
    sent input for a minute and no agent holds the lease; ``watch`` otherwise."""
    is_recent = inputs.seconds_since_input is not None and inputs.seconds_since_input <= CONTROL_INPUT_WINDOW_SECONDS
    is_long_quiet = inputs.seconds_since_input is None or inputs.seconds_since_input >= IDLE_AFTER_SECONDS
    if inputs.controller == Controller.HUMAN and is_recent:
        return VIDEO_ID_CONTROL
    if inputs.controller != Controller.AGENT and is_long_quiet:
        return VIDEO_ID_IDLE
    return VIDEO_ID_WATCH
