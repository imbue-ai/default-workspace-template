from share_gateway.origin_policy import is_request_origin_allowed

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"
_AUTH_LABEL = "auth-x7k9q2w1"
_LABELS = {"web-1a2b3c4d": "web"}
_OWN_ORIGIN = f"https://web-1a2b3c4d.{_DOMAIN}"


def _allowed(method: str, origin: str | None, is_ws: bool) -> bool:
    return is_request_origin_allowed(method, origin, is_ws, _DOMAIN, _LABELS, _AUTH_LABEL)


def test_websocket_upgrades_require_a_workspace_origin() -> None:
    assert _allowed("GET", _OWN_ORIGIN, True) is True
    assert _allowed("GET", "https://evil.example.com", True) is False
    assert _allowed("GET", None, True) is False


def test_plain_gets_are_exempt() -> None:
    assert _allowed("GET", "https://evil.example.com", False) is True
    assert _allowed("HEAD", None, False) is True


def test_non_get_rejects_present_foreign_origin_but_allows_absent() -> None:
    assert _allowed("POST", "https://evil.example.com", False) is False
    assert _allowed("POST", _OWN_ORIGIN, False) is True
    assert _allowed("POST", None, False) is True
    assert _allowed("DELETE", "https://evil.example.com", False) is False
