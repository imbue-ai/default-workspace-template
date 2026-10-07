import json

import pytest

from activity.request_guard import is_owner_request
from activity.request_guard import is_write_allowed

_LABEL = "activity-as27k3mv"
_DESKTOP_ORIGIN = f"https://{_LABEL}.agent-1f2e3d.localhost:8421"
_JSON = "application/json"


@pytest.mark.parametrize(
    ("method", "origin", "fetch_site", "content_type", "is_allowed"),
    [
        # The desktop's own page: the forwarder has dropped Host, but the browser says the request is same-origin.
        ("POST", _DESKTOP_ORIGIN, "same-origin", _JSON, True),
        ("POST", _DESKTOP_ORIGIN, "same-origin", "application/json; charset=utf-8", True),
        # Another workspace app's page, and a foreign site: the browser says so.
        ("POST", "https://files-ab12cd34.agent-1f2e3d.localhost:8421", "same-site", _JSON, False),
        ("POST", "https://elsewhere.example", "cross-site", _JSON, False),
        # No Origin: curl or an agent's script inside the workspace.
        ("POST", None, None, _JSON, True),
        # Not JSON: a form or a no-cors fetch from anywhere.
        ("POST", _DESKTOP_ORIGIN, "same-origin", None, False),
        ("POST", _DESKTOP_ORIGIN, "same-origin", "text/plain", False),
        # A browser too old to send Sec-Fetch-Site: the Origin's first label must be this app's.
        ("POST", _DESKTOP_ORIGIN, None, _JSON, True),
        ("PUT", "https://files-ab12cd34.agent-1f2e3d.localhost:8421", None, _JSON, False),
        # Reads are never refused here.
        ("GET", "https://elsewhere.example", "cross-site", None, True),
    ],
)
def test_a_write_must_be_json_from_this_apps_own_page(
    method: str, origin: str | None, fetch_site: str | None, content_type: str | None, is_allowed: bool
) -> None:
    assert is_write_allowed(method, origin, fetch_site, content_type, _LABEL) is is_allowed


def test_without_a_known_label_an_old_browsers_write_is_refused() -> None:
    assert is_write_allowed("POST", _DESKTOP_ORIGIN, None, _JSON, None) is False


@pytest.mark.parametrize(
    ("header", "is_owner"),
    [
        (None, True),
        (json.dumps({"owner": True, "user_id": "u1", "email": "me@example.com"}), True),
        (json.dumps({"owner": False, "user_id": "u2", "email": "guest@example.com"}), False),
        # pydantic's bool parsing, as the shell's: a stringly or numerically false owner flag is a visitor.
        (json.dumps({"owner": "false"}), False),
        (json.dumps({"owner": 0}), False),
        ("", True),
        ("not json", True),
        (json.dumps(["owner"]), True),
        (json.dumps({"user_id": "u3"}), True),
    ],
)
def test_only_an_explicit_visitor_identity_is_not_the_owner(header: str | None, is_owner: bool) -> None:
    assert is_owner_request(header) is is_owner
