from activity.replies import refusal_reason


def test_a_refusal_shows_the_other_apps_detail_else_its_body() -> None:
    assert refusal_reason('{"detail": "converging"}') == "converging"
    assert refusal_reason("plain text") == "plain text"
    assert refusal_reason('["a"]') == '["a"]'
