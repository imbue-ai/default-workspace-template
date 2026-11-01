import pytest
from flask import Flask
from workspace_layout.identity import IDENTITY_HEADER

from imbue.chat.identity import UnreadableIdentityHeaderError
from imbue.chat.identity import is_owner_identity
from imbue.chat.identity import request_identity
from imbue.chat.identity import request_participant
from imbue.chat.request_helpers import answer_chat_app_error


@pytest.mark.parametrize(
    ("header_value", "is_owner"),
    [
        pytest.param(None, True, id="no-header"),
        pytest.param("  ", True, id="blank"),
        pytest.param('{"owner": true}', True, id="owner"),
        pytest.param('{"owner": true, "user_id": "u-1", "email": "o@example.com"}', True, id="owner-with-account"),
        pytest.param('{"owner": false, "user_id": "u-2", "email": "v@example.com"}', False, id="visitor"),
    ],
)
def test_only_a_header_that_says_owner_false_is_a_visitor(header_value: str | None, is_owner: bool) -> None:
    assert is_owner_identity(header_value) is is_owner


@pytest.mark.parametrize(
    "header_value",
    [
        pytest.param("not json", id="unreadable"),
        pytest.param('{"owner": "false"}', id="owner-not-a-boolean"),
        pytest.param("[false]", id="not-an-object"),
    ],
)
def test_a_present_header_that_does_not_parse_is_refused_rather_than_read_as_the_owner(header_value: str) -> None:
    with pytest.raises(UnreadableIdentityHeaderError):
        is_owner_identity(header_value)


def _identity_app() -> Flask:
    """A route reading the request's identity the way every chat route does, under the app's own error handler."""
    app = Flask(__name__)
    app.register_error_handler(Exception, answer_chat_app_error)
    app.add_url_rule("/me", endpoint="me", view_func=lambda: {"participant": str(request_participant())})
    app.add_url_rule("/identity", endpoint="identity", view_func=lambda: request_identity().model_dump(mode="json"))
    return app


def test_a_route_answers_400_for_an_unreadable_header_and_the_anonymous_owner_for_none() -> None:
    client = _identity_app().test_client()

    assert client.get("/me").get_json() == {"participant": "owner"}
    assert (
        client.get(
            "/identity", headers={IDENTITY_HEADER: '{"owner": false, "user_id": "u-2", "email": "v@x"}'}
        ).get_json()["user_id"]
        == "u-2"
    )

    for path in ("/me", "/identity"):
        refused = client.get(path, headers={IDENTITY_HEADER: '{"owner": "false"}'})
        assert refused.status_code == 400
        assert IDENTITY_HEADER in refused.get_json()["detail"]
