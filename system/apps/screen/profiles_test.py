import pytest

from screen.data_types import Controller
from screen.profiles import ProfileInputs
from screen.profiles import choose_video_id


@pytest.mark.parametrize(
    ("controller", "seconds_since_input", "expected"),
    [
        (Controller.HUMAN, 0.0, "control"),
        (Controller.HUMAN, 10.0, "control"),
        (Controller.HUMAN, 10.5, "watch"),
        (Controller.HUMAN, 59.0, "watch"),
        (Controller.HUMAN, 60.0, "idle"),
        (Controller.HUMAN, None, "idle"),
        (Controller.NOBODY, 0.0, "watch"),
        (Controller.NOBODY, 59.9, "watch"),
        (Controller.NOBODY, 60.0, "idle"),
        (Controller.NOBODY, None, "idle"),
        (Controller.AGENT, 0.0, "watch"),
        (Controller.AGENT, 5.0, "watch"),
        (Controller.AGENT, 600.0, "watch"),
        (Controller.AGENT, None, "watch"),
    ],
)
def test_the_profile_follows_the_controller_and_the_input_clock(
    controller: Controller, seconds_since_input: float | None, expected: str
) -> None:
    assert choose_video_id(ProfileInputs(controller=controller, seconds_since_input=seconds_since_input)) == expected
