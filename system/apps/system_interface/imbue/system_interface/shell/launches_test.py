import pytest

from imbue.system_interface.shell.errors import LaunchUnavailableError
from imbue.system_interface.shell.launches import LaunchPost
from imbue.system_interface.shell.launches import post_launch
from imbue.system_interface.testing import find_free_port


def test_a_launch_no_app_answers_carries_the_reason_and_no_sentence_of_the_shells_own() -> None:
    post = LaunchPost(app="buddy", url=f"http://127.0.0.1:{find_free_port()}/api/intake", body={"message": "hi"})

    with pytest.raises(LaunchUnavailableError) as raised:
        post_launch(post)

    assert raised.value.user_facing_message is None
    assert raised.value.reportable is None
    assert str(raised.value).startswith("buddy could not be reached for the launch: ")
