"""Gate mode (an auth sandbox's Screen, reached over Modal's TLS): loopback is the owner, every other request needs
the owner session the owner token opens, on every route, the WebSockets and the neko proxy included."""

import http.client
import json
import logging
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from click.testing import CliRunner
from flask import Flask
from flask.testing import FlaskClient
from loguru import logger
from pydantic import SecretBytes
from werkzeug.datastructures import MultiDict
from werkzeug.test import TestResponse

from screen.errors import ScreenConfigurationError
from screen.main import main
from screen.owner_gate import OWNER_SESSION_COOKIE_NAME
from screen.owner_gate import OwnerGate
from screen.owner_gate import PARTITIONED_OWNER_SESSION_COOKIE_NAME
from screen.owner_gate import has_owner_session
from screen.owner_gate import is_loopback_host
from screen.owner_gate import location_without_owner_token
from screen.owner_gate import merge_frame_ancestors
from screen.owner_gate import owner_gate_from_environ
from screen.owner_gate import owner_session_cookie_value
from screen.owner_gate import request_target_with_owner_token_redacted
from screen.service import ScreenService
from screen.serving import serve_in_background
from testing import start_service_stack

# What a Modal tunnel's traffic arrives from inside the sandbox: not loopback, and with no forwarding headers.
_TUNNEL: Final[dict[str, str]] = {"REMOTE_ADDR": "172.20.0.1"}
_VISITOR: Final[dict[str, str]] = {
    "X-Imbue-Identity": json.dumps({"owner": False, "user_id": "u2", "email": "mia@example.com"})
}
_WEBSOCKET_UPGRADE: Final[dict[str, str]] = {
    "Upgrade": "websocket",
    "Connection": "Upgrade",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version": "13",
}


def _owner_cookie(owner_gate: OwnerGate, name: str = OWNER_SESSION_COOKIE_NAME) -> dict[str, str]:
    return {"Cookie": f"{name}={owner_session_cookie_value(owner_gate)}"}


def _set_cookies_by_name(response: TestResponse) -> dict[str, str]:
    return {header.split("=", 1)[0]: header for header in response.headers.getlist("Set-Cookie")}


def test_gate_mode_is_off_without_a_digest_file_and_refuses_a_malformed_setting(tmp_path: Path) -> None:
    assert owner_gate_from_environ({}) is None
    digest_path = tmp_path / "absent-until-claimed"
    gate = owner_gate_from_environ(
        {"SCREEN_OWNER_TOKEN_SHA256_FILE": str(digest_path), "SCREEN_FRAME_ANCESTORS": " https://c.test "}
    )
    assert gate is not None
    assert gate.owner_token_sha256_path == digest_path
    assert gate.frame_ancestors == "https://c.test"
    # Each start keys its sessions afresh.
    again = owner_gate_from_environ(
        {"SCREEN_OWNER_TOKEN_SHA256_FILE": str(digest_path), "SCREEN_FRAME_ANCESTORS": "https://c.test"}
    )
    assert again is not None
    assert owner_session_cookie_value(again) != owner_session_cookie_value(gate)
    for bad_environ in (
        {"SCREEN_OWNER_TOKEN_SHA256_FILE": str(digest_path)},
        {"SCREEN_OWNER_TOKEN_SHA256_FILE": str(digest_path), "SCREEN_FRAME_ANCESTORS": "https://c.test; script-src *"},
    ):
        with pytest.raises(ScreenConfigurationError):
            owner_gate_from_environ(bad_environ)


def test_the_service_refuses_to_serve_beyond_loopback_without_the_gate() -> None:
    result = CliRunner().invoke(
        main, [], env={"SCREEN_LISTEN_HOST": "0.0.0.0", "SCREEN_OWNER_TOKEN_SHA256_FILE": None}, catch_exceptions=True
    )
    assert isinstance(result.exception, ScreenConfigurationError)
    assert "serving on 0.0.0.0 needs the owner gate" in str(result.exception)


def test_a_loopback_request_is_the_owner_without_a_session(gated_client: FlaskClient) -> None:
    assert gated_client.get("/health").status_code == 200
    assert gated_client.post("/api/start").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/health"),
        ("GET", "/api/state"),
        ("GET", "/api/connect-info"),
        ("GET", "/api/schema"),
        ("POST", "/api/session"),
        ("POST", "/api/start"),
        ("GET", "/neko/api/room/screen/cast.jpg"),
        ("POST", "/neko/api/login"),
        ("GET", "/no/such/route"),
    ],
)
def test_a_tunnelled_request_without_a_session_is_refused(gated_client: FlaskClient, method: str, path: str) -> None:
    answer = gated_client.open(path, method=method, environ_base=_TUNNEL)
    assert answer.status_code == 401
    assert answer.get_json() == {"detail": "an owner session is required"}
    assert "Location" not in answer.headers


@pytest.mark.parametrize("path", ["/api/ws", "/neko/api/ws", "/neko/api/ws?token=a-neko-token"])
def test_a_tunnelled_websocket_upgrade_without_a_session_is_refused(gated_client: FlaskClient, path: str) -> None:
    answer = gated_client.get(path, headers=_WEBSOCKET_UPGRADE, environ_base=_TUNNEL)
    assert answer.status_code == 401
    assert "Sec-WebSocket-Accept" not in answer.headers


@pytest.mark.parametrize(
    ("method", "path", "fetch_metadata"),
    [
        ("POST", "/api/run", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}),
        ("POST", "/api/run", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}),
        ("POST", "/api/run", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"}),
        ("GET", "/api/state", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}),
        ("GET", "/api/ws", {**_WEBSOCKET_UPGRADE, "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "websocket"}),
        ("GET", "/neko/api/ws", {**_WEBSOCKET_UPGRADE, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "websocket"}),
    ],
)
def test_a_request_from_another_origin_is_refused_despite_the_session(
    gated_client: FlaskClient, owner_gate: OwnerGate, method: str, path: str, fetch_metadata: dict[str, str]
) -> None:
    # Another *.modal.host page is same-site with the Screen, so the browser sends the owner session with it.
    headers = {**_owner_cookie(owner_gate), **fetch_metadata}
    answer = gated_client.open(path, method=method, headers=headers, environ_base=_TUNNEL)
    assert answer.status_code == 403
    assert answer.get_json() == {"detail": "only the Screen's own pages may call it"}
    assert "Sec-WebSocket-Accept" not in answer.headers


def test_a_loopback_request_from_another_origin_is_refused(gated_client: FlaskClient) -> None:
    # A page the sandbox's sign-in browser shows reaches the Screen over loopback.
    cross_site = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
    answer = gated_client.post("/api/run", data='{"command": ["true"]}', headers=cross_site)
    assert answer.status_code == 403
    assert answer.get_json() == {"detail": "only the Screen's own pages may call it"}


@pytest.mark.parametrize("host", ["127.0.0.1:8090", "localhost:8090", "[::1]:8090", "LOCALHOST", "127.0.0.1"])
def test_a_loopback_request_naming_loopback_is_the_owner(gated_client: FlaskClient, host: str) -> None:
    assert gated_client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1.nip.io:8090", "rebound.example:8090", "[::1:8090"])
def test_a_loopback_request_under_another_name_needs_the_session(
    gated_client: FlaskClient, owner_gate: OwnerGate, host: str
) -> None:
    # A plain http page that reaches loopback through a name of its own sends no fetch metadata.
    answer = gated_client.post("/api/run", data='{"command": ["true"]}', headers={"Host": host})
    assert answer.status_code == 401
    assert answer.get_json() == {"detail": "an owner session is required"}
    assert gated_client.get("/health", headers={"Host": host, **_owner_cookie(owner_gate)}).status_code == 200


def test_own_origin_requests_and_navigations_from_elsewhere_are_admitted(
    gated_client: FlaskClient, owner_gate: OwnerGate, owner_token: str
) -> None:
    session = _owner_cookie(owner_gate)
    same_origin = {**session, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert gated_client.get("/health", headers=same_origin, environ_base=_TUNNEL).status_code == 200
    # The chrome's frame of the Screen, its redirect, and the tab the chrome opens are cross-site navigations.
    navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    traded = gated_client.get(f"/?owner_token={owner_token}", headers=navigation, environ_base=_TUNNEL)
    assert traded.status_code == 302
    assert gated_client.get("/health", headers={**session, **navigation}, environ_base=_TUNNEL).status_code == 200
    typed = {**session, "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}
    assert gated_client.get("/health", headers=typed, environ_base=_TUNNEL).status_code == 200


def test_the_owner_token_sets_both_session_cookies_and_redirects_without_itself(
    gated_client: FlaskClient, owner_gate: OwnerGate, owner_token: str
) -> None:
    answer = gated_client.get(f"/?owner_token={owner_token}&strip=off", environ_base=_TUNNEL)

    assert answer.status_code == 302
    assert answer.headers["Location"] == "/?strip=off"
    assert answer.headers["Cache-Control"] == "no-store"
    set_cookies = _set_cookies_by_name(answer)
    assert set(set_cookies) == {OWNER_SESSION_COOKIE_NAME, PARTITIONED_OWNER_SESSION_COOKIE_NAME}
    for header in set_cookies.values():
        assert f"={owner_session_cookie_value(owner_gate)};" in header
        assert "Secure" in header
        assert "HttpOnly" in header
        assert "Path=/" in header
        assert "Domain=" not in header
    assert "SameSite=Lax" in set_cookies[OWNER_SESSION_COOKIE_NAME]
    assert "Partitioned" not in set_cookies[OWNER_SESSION_COOKIE_NAME]
    assert "SameSite=None" in set_cookies[PARTITIONED_OWNER_SESSION_COOKIE_NAME]
    assert "Partitioned" in set_cookies[PARTITIONED_OWNER_SESSION_COOKIE_NAME]

    # The token stays good for the sandbox's life: an iframe reload presents it again.
    again = gated_client.get(f"/api/state?owner_token={owner_token}", environ_base=_TUNNEL)
    assert again.status_code == 302
    assert again.headers["Location"] == "/api/state"


@pytest.mark.parametrize("unclaimed_content", [None, "", "\n"])
def test_an_unclaimed_screen_admits_loopback_alone(
    gated_client: FlaskClient,
    owner_gate: OwnerGate,
    owner_token: str,
    owner_token_sha256_path: Path,
    unclaimed_content: str | None,
) -> None:
    claimed_digest = owner_token_sha256_path.read_text()
    if unclaimed_content is None:
        owner_token_sha256_path.unlink()
    else:
        owner_token_sha256_path.write_text(unclaimed_content)

    assert gated_client.get("/health").status_code == 200
    assert gated_client.get("/health", headers=_owner_cookie(owner_gate), environ_base=_TUNNEL).status_code == 401
    traded = gated_client.get(f"/?owner_token={owner_token}", environ_base=_TUNNEL)
    assert traded.status_code == 401
    assert traded.headers.getlist("Set-Cookie") == []

    # The digest is read per request: the claim's write admits the owner with no restart.
    owner_token_sha256_path.write_text(claimed_digest)
    assert gated_client.get(f"/?owner_token={owner_token}", environ_base=_TUNNEL).status_code == 302
    assert gated_client.get("/health", headers=_owner_cookie(owner_gate), environ_base=_TUNNEL).status_code == 200


@pytest.mark.parametrize("malformed", ["A" * 64, "0" * 63, "not a digest"])
def test_a_malformed_digest_file_admits_loopback_alone_and_is_logged(
    gated_client: FlaskClient,
    owner_gate: OwnerGate,
    owner_token: str,
    owner_token_sha256_path: Path,
    malformed: str,
) -> None:
    owner_token_sha256_path.write_text(malformed)
    errors: list[str] = []
    handle = logger.add(lambda message: errors.append(message.record["message"]), level="ERROR")
    try:
        session = gated_client.get("/health", headers=_owner_cookie(owner_gate), environ_base=_TUNNEL)
        traded = gated_client.get(f"/?owner_token={owner_token}", environ_base=_TUNNEL)
        loopback = gated_client.get("/health")
    finally:
        logger.remove(handle)
    assert session.status_code == 401
    assert traded.status_code == 401
    assert loopback.status_code == 200
    assert errors == [f"The owner token digest at {owner_token_sha256_path} is not 64 lower-case hex digits"] * 2


def test_a_wrong_owner_token_opens_no_session(gated_client: FlaskClient, owner_token: str) -> None:
    for query in ("owner_token=not-the-token", f"owner_token={owner_token}&owner_token=not-the-token"):
        answer = gated_client.get(f"/?{query}", environ_base=_TUNNEL)
        assert answer.status_code == 401
        assert answer.get_json() == {"detail": "the owner token is not this Screen's"}
        assert answer.headers.getlist("Set-Cookie") == []


def test_the_owner_token_is_traded_only_on_a_get(gated_client: FlaskClient, owner_token: str) -> None:
    answer = gated_client.post(f"/api/start?owner_token={owner_token}", environ_base=_TUNNEL)
    assert answer.status_code == 401
    assert answer.headers.getlist("Set-Cookie") == []


def test_the_redirect_never_names_another_host(gated_client: FlaskClient, owner_token: str) -> None:
    # A request line of ``GET //evil.example/x`` arrives as this path.
    answer = gated_client.get(
        f"/?owner_token={owner_token}", environ_base=_TUNNEL, environ_overrides={"PATH_INFO": "//evil.example/x"}
    )
    assert answer.status_code == 302
    assert answer.headers["Location"] == "/evil.example/x"


@pytest.mark.parametrize("cookie_name", [OWNER_SESSION_COOKIE_NAME, PARTITIONED_OWNER_SESSION_COOKIE_NAME])
def test_either_session_cookie_admits_a_tunnelled_request(
    gated_client: FlaskClient, owner_gate: OwnerGate, cookie_name: str
) -> None:
    headers = _owner_cookie(owner_gate, cookie_name)
    assert gated_client.get("/health", headers=headers, environ_base=_TUNNEL).status_code == 200
    # Past the gate the neko proxy answers for itself: neko is not running here.
    neko = gated_client.get("/neko/api/room/screen/cast.jpg", headers=headers, environ_base=_TUNNEL)
    assert neko.status_code == 502


def test_a_foreign_cookie_value_ahead_of_the_session_does_not_mask_it(
    gated_client: FlaskClient, owner_gate: OwnerGate
) -> None:
    real = owner_session_cookie_value(owner_gate)
    shadowed = {"Cookie": f"{OWNER_SESSION_COOKIE_NAME}=set-by-another-modal-page; {OWNER_SESSION_COOKIE_NAME}={real}"}
    assert gated_client.get("/health", headers=shadowed, environ_base=_TUNNEL).status_code == 200
    forged_only = {"Cookie": f"{OWNER_SESSION_COOKIE_NAME}=set-by-another-modal-page"}
    assert gated_client.get("/health", headers=forged_only, environ_base=_TUNNEL).status_code == 401
    # Only the two session names count, whatever another cookie carries.
    elsewhere = MultiDict([(PARTITIONED_OWNER_SESSION_COOKIE_NAME, "x"), ("other", real)])
    assert not has_owner_session(owner_gate, elsewhere)


@pytest.mark.parametrize("foreign_value", ["\xc3\xa9", '"\\351"'])
def test_a_non_ascii_foreign_cookie_value_neither_masks_the_session_nor_fails_the_request(
    gated_client: FlaskClient, owner_gate: OwnerGate, foreign_value: str
) -> None:
    real = owner_session_cookie_value(owner_gate)
    shadowed = {"Cookie": f"{OWNER_SESSION_COOKIE_NAME}={foreign_value}; {OWNER_SESSION_COOKIE_NAME}={real}"}
    assert gated_client.get("/health", headers=shadowed, environ_base=_TUNNEL).status_code == 200
    forged_only = {"Cookie": f"{OWNER_SESSION_COOKIE_NAME}={foreign_value}"}
    assert gated_client.get("/health", headers=forged_only, environ_base=_TUNNEL).status_code == 401
    assert not has_owner_session(owner_gate, MultiDict([(OWNER_SESSION_COOKIE_NAME, "\u00e9")]))


def test_a_session_from_another_start_is_refused(gated_client: FlaskClient, owner_gate: OwnerGate) -> None:
    other_start = owner_gate.model_copy(update={"session_secret": SecretBytes(uuid4().bytes)})
    headers = _owner_cookie(other_start)
    assert gated_client.get("/health", headers=headers, environ_base=_TUNNEL).status_code == 401


def test_in_gate_mode_the_identity_header_is_ignored_and_the_session_is_the_owner(
    gated_client: FlaskClient, owner_gate: OwnerGate, service: ScreenService
) -> None:
    headers = {**_owner_cookie(owner_gate), **_VISITOR}
    # A visitor is refused every owner route outside gate mode; here the gated request is the owner.
    assert gated_client.get("/api/windows", headers=headers, environ_base=_TUNNEL).status_code != 403
    start_service_stack(service)
    session = gated_client.post("/api/session", headers=headers, environ_base=_TUNNEL)
    assert session.status_code == 200, session.get_json()
    assert session.get_json()["is_owner"] is True
    assert session.get_json()["name"] == "Owner"


def test_every_gated_response_carries_the_frame_ancestors_policy(
    gated_client: FlaskClient, owner_gate: OwnerGate, owner_token: str
) -> None:
    expected = "frame-ancestors https://chrome.example.test"
    assert gated_client.get("/health").headers["Content-Security-Policy"] == expected
    assert gated_client.get("/health", environ_base=_TUNNEL).headers["Content-Security-Policy"] == expected
    redirect = gated_client.get(f"/?owner_token={owner_token}", environ_base=_TUNNEL)
    assert redirect.headers["Content-Security-Policy"] == expected


def test_outside_gate_mode_no_response_carries_a_policy_and_no_session_is_needed(app: Flask) -> None:
    client = app.test_client()
    answer = client.get("/health", environ_base=_TUNNEL)
    assert answer.status_code == 200
    assert "Content-Security-Policy" not in answer.headers


def test_merge_frame_ancestors_replaces_or_appends_the_directive() -> None:
    assert merge_frame_ancestors(None, "https://c.test") == "frame-ancestors https://c.test"
    assert merge_frame_ancestors("  ", "https://c.test") == "frame-ancestors https://c.test"
    assert (
        merge_frame_ancestors("default-src 'self'; img-src data:", "https://c.test")
        == "default-src 'self'; img-src data:; frame-ancestors https://c.test"
    )
    assert (
        merge_frame_ancestors("Frame-Ancestors *; default-src 'self';", "https://c.test")
        == "default-src 'self'; frame-ancestors https://c.test"
    )


def test_location_without_owner_token_keeps_the_rest_of_the_query() -> None:
    assert location_without_owner_token("/", (("owner_token", "t"),)) == "/"
    assert location_without_owner_token("/a b", (("x", "1"), ("owner_token", "t"), ("x", "2"))) == "/a%20b?x=1&x=2"
    assert location_without_owner_token("///evil.example", ()) == "/evil.example"


def test_request_target_with_owner_token_redacted_keeps_everything_but_the_token() -> None:
    assert request_target_with_owner_token_redacted("/a?x=1") == "/a?x=1"
    assert request_target_with_owner_token_redacted("/") == "/"
    assert (
        request_target_with_owner_token_redacted("/a?x=1&owner_token=t%2B1&owner_token=t2")
        == "/a?x=1&owner_token=REDACTED&owner_token=REDACTED"
    )


def test_the_access_log_never_shows_the_owner_token(
    gated_app: Flask, owner_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="werkzeug")
    with serve_in_background("127.0.0.1", 0, gated_app) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.socket.getsockname()[1], timeout=5)
        try:
            connection.request("GET", f"/?owner_token={owner_token}&x=1")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
    assert response.status == 302
    assert "GET /?owner_token=REDACTED&x=1 HTTP/1.1" in caplog.text
    assert owner_token not in caplog.text


def test_is_loopback_host_reads_the_name_without_its_port() -> None:
    assert is_loopback_host("127.0.0.1:8090")
    assert is_loopback_host("[::1]")
    assert is_loopback_host("Localhost:1")
    assert not is_loopback_host("127.0.0.1.nip.io")
    assert not is_loopback_host("localhost.example:8090")
    assert not is_loopback_host("[::1")
    assert not is_loopback_host("")
