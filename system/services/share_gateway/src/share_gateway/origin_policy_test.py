from share_gateway.origin_policy import is_request_origin_allowed

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def test_websocket_upgrades_require_a_workspace_origin() -> None:
    assert is_request_origin_allowed("GET", f"https://web.{_DOMAIN}", True, _DOMAIN) is True
    assert is_request_origin_allowed("GET", "https://evil.example.com", True, _DOMAIN) is False
    assert is_request_origin_allowed("GET", None, True, _DOMAIN) is False


def test_plain_gets_are_exempt() -> None:
    assert is_request_origin_allowed("GET", "https://evil.example.com", False, _DOMAIN) is True
    assert is_request_origin_allowed("HEAD", None, False, _DOMAIN) is True


def test_non_get_rejects_present_foreign_origin_but_allows_absent() -> None:
    assert is_request_origin_allowed("POST", "https://evil.example.com", False, _DOMAIN) is False
    assert is_request_origin_allowed("POST", f"https://{_DOMAIN}", False, _DOMAIN) is True
    assert is_request_origin_allowed("POST", None, False, _DOMAIN) is True
    assert is_request_origin_allowed("DELETE", "https://evil.example.com", False, _DOMAIN) is False
