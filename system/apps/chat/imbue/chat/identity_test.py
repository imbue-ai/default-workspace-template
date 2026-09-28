import pytest

from imbue.chat.identity import is_owner_identity


@pytest.mark.parametrize(
    ("header_value", "is_owner"),
    [
        pytest.param(None, True, id="no-header"),
        pytest.param("  ", True, id="blank"),
        pytest.param('{"owner": true}', True, id="owner"),
        pytest.param('{"owner": true, "user_id": "u-1", "email": "o@example.com"}', True, id="owner-with-account"),
        pytest.param('{"owner": false, "user_id": "u-2", "email": "v@example.com"}', False, id="visitor"),
        pytest.param("not json", True, id="unreadable"),
        pytest.param('{"owner": "false"}', True, id="owner-not-a-boolean"),
        pytest.param("[false]", True, id="not-an-object"),
    ],
)
def test_only_a_header_that_says_owner_false_is_a_visitor(header_value: str | None, is_owner: bool) -> None:
    assert is_owner_identity(header_value) is is_owner
