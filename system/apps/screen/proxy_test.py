from screen.proxy import LoginRateLimiter
from screen.proxy import forwardable_headers


def test_hop_by_hop_headers_and_the_origin_stay_behind() -> None:
    headers = [
        ("Host", "screen.host-1.localhost"),
        ("Connection", "keep-alive"),
        ("Origin", "http://screen.host-1.localhost"),
        ("Content-Length", "12"),
        ("Authorization", "Bearer x"),
        ("Content-Type", "application/json"),
        ("X-Imbue-Identity", "{}"),
    ]
    assert forwardable_headers(iter(headers)) == {
        "Authorization": "Bearer x",
        "Content-Type": "application/json",
        "X-Imbue-Identity": "{}",
    }


def test_login_limiter_allows_a_window_of_attempts_then_refuses_until_it_slides() -> None:
    limiter = LoginRateLimiter(limit=3, window_seconds=60.0)
    assert [limiter.allow(now) for now in (0.0, 1.0, 2.0)] == [True, True, True]
    assert limiter.allow(3.0) is False
    assert limiter.allow(59.9) is False
    assert limiter.allow(60.5) is True
