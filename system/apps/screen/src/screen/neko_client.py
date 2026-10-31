"""neko over its REST API and its WebSocket (docs/system/specs/screen/spec.md section 2.3): the service's REST calls
carry the API token, a human's login answers the token its viewer then uses, and the service's own admin session
follows ``control/host`` and ``session/*`` on the WebSocket."""

import json
import threading
import time
from collections.abc import Callable
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as connect_websocket

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.data_types import NekoSession
from screen.data_types import NekoSessionRecord
from screen.data_types import ScreenSize
from screen.errors import NekoAuthenticationError
from screen.errors import NekoError
from screen.interfaces import NekoInterface
from screen.primitives import DisplayName
from screen.primitives import NEKO_HOST
from screen.primitives import NEKO_PORT
from screen.primitives import NekoSessionId
from screen.primitives import NekoToken

NEKO_BASE_URL: Final[str] = f"http://{NEKO_HOST}:{NEKO_PORT}"
NEKO_WEBSOCKET_URL: Final[str] = f"ws://{NEKO_HOST}:{NEKO_PORT}/api/ws"

# Loopback round trips; a screenshot encodes a 1080p JPEG, the rest is instant.
_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
_SCREENSHOT_TIMEOUT_SECONDS: Final[float] = 30.0
_HEALTH_TIMEOUT_SECONDS: Final[float] = 2.0

_EVENT_RECONNECT_SECONDS: Final[float] = 2.0
_EVENT_RECEIVE_TIMEOUT_SECONDS: Final[float] = 30.0

NekoEventHandler = Callable[[str, dict[str, Any]], None]


@pure
def parse_login_answer(document: dict[str, Any], name: DisplayName, is_owner: bool) -> NekoSession:
    """The session neko's login route answered (``{id, token, profile: {name, is_admin, ...}, state}``)."""
    session_id = document.get("id")
    token = document.get("token")
    if not isinstance(session_id, str) or not session_id or not isinstance(token, str) or not token:
        raise NekoAuthenticationError("neko's login answered no session id or token (cookies must stay disabled)")
    profile = document.get("profile")
    is_admin = isinstance(profile, dict) and profile.get("is_admin") is True
    return NekoSession(
        session_id=NekoSessionId(session_id), token=NekoToken(token), name=name, is_owner=is_owner, is_admin=is_admin
    )


@pure
def parse_session_records(document: Any) -> list[NekoSessionRecord]:
    """The sessions neko's list route answered; an entry off the shape is skipped."""
    if not isinstance(document, list):
        raise NekoError("neko's sessions route did not answer a list")
    records: list[NekoSessionRecord] = []
    for entry in document:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
        records.append(
            NekoSessionRecord(
                session_id=NekoSessionId(entry["id"]),
                name=str(profile.get("name", "")),
                is_admin=profile.get("is_admin") is True,
                is_connected=state.get("is_connected") is True,
                not_connected_since=state.get("not_connected_since"),
            )
        )
    return records


class NekoClient(NekoInterface):
    """The REST client; ``api_token`` is this stack start's token, set when the stack comes up."""

    base_url: str = Field(default=NEKO_BASE_URL, frozen=True, description="neko's HTTP origin")
    api_token: SecretStr | None = Field(default=None, description="The API token for the admin routes")

    def set_api_token(self, token: SecretStr | None) -> None:
        self.api_token = token

    def _admin_headers(self) -> dict[str, str]:
        if self.api_token is None:
            raise NekoError("the stack has not started, so there is no API token to speak to neko with")
        return {"Authorization": f"Bearer {self.api_token.get_secret_value()}"}

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        params: dict[str, str] | None,
        timeout_seconds: float,
    ) -> httpx.Response:
        try:
            response = httpx.request(
                method,
                self.base_url + path,
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as e:
            raise NekoError(f"neko could not be reached for {method} {path}: {e}") from e
        if response.status_code == 401 or response.status_code == 403:
            raise NekoAuthenticationError(f"neko refused {method} {path}: {response.status_code} {response.text}")
        if response.status_code >= 400:
            raise NekoError(f"neko answered {method} {path} with {response.status_code}: {response.text}")
        return response

    def is_healthy(self) -> bool:
        try:
            response = httpx.get(self.base_url + "/health", timeout=_HEALTH_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def login(self, username: str, password: str, is_owner: bool) -> NekoSession:
        response = self._request(
            "POST",
            "/api/login",
            {},
            {"username": username, "password": password},
            None,
            _REQUEST_TIMEOUT_SECONDS,
        )
        return parse_login_answer(response.json(), DisplayName(username), is_owner)

    def logout(self, token: NekoToken) -> None:
        self._request(
            "POST", "/api/logout", {"Authorization": f"Bearer {token}"}, None, None, _REQUEST_TIMEOUT_SECONDS
        )

    def set_locked_controls(self, is_locked: bool) -> None:
        # neko merges the body onto its current settings, so one key is a partial update.
        self._request(
            "POST",
            "/api/room/settings",
            self._admin_headers(),
            {"locked_controls": is_locked},
            None,
            _REQUEST_TIMEOUT_SECONDS,
        )

    def give_control(self, session_id: NekoSessionId) -> None:
        self._request(
            "POST", f"/api/room/control/give/{session_id}", self._admin_headers(), None, None, _REQUEST_TIMEOUT_SECONDS
        )

    def reset_control(self) -> None:
        self._request("POST", "/api/room/control/reset", self._admin_headers(), None, None, _REQUEST_TIMEOUT_SECONDS)

    def screenshot_jpeg(self, quality: int) -> bytes:
        response = self._request(
            "GET",
            "/api/room/screen/shot.jpg",
            self._admin_headers(),
            None,
            {"quality": str(quality)},
            _SCREENSHOT_TIMEOUT_SECONDS,
        )
        return response.content

    def screen_size(self) -> ScreenSize:
        response = self._request(
            "GET", "/api/room/screen", self._admin_headers(), None, None, _REQUEST_TIMEOUT_SECONDS
        )
        return ScreenSize.model_validate(response.json())

    def set_screen_size(self, size: ScreenSize) -> ScreenSize:
        response = self._request(
            "POST",
            "/api/room/screen",
            self._admin_headers(),
            {"width": size.width, "height": size.height, "rate": size.rate},
            None,
            _REQUEST_TIMEOUT_SECONDS,
        )
        return ScreenSize.model_validate(response.json())

    def list_sessions(self) -> list[NekoSessionRecord]:
        response = self._request("GET", "/api/sessions", self._admin_headers(), None, None, _REQUEST_TIMEOUT_SECONDS)
        return parse_session_records(response.json())

    def delete_session(self, session_id: NekoSessionId) -> None:
        self._request(
            "DELETE", f"/api/sessions/{session_id}", self._admin_headers(), None, None, _REQUEST_TIMEOUT_SECONDS
        )


class NekoEventSubscriber(MutableModel):
    """The service's own WebSocket session on neko, delivering every event to a handler on its own thread; it
    reconnects while running and stops on request."""

    model_config = {"arbitrary_types_allowed": True}

    websocket_url: str = Field(default=NEKO_WEBSOCKET_URL, frozen=True, description="neko's WebSocket")
    token: SecretStr = Field(frozen=True, description="The admin session's token")
    handler: NekoEventHandler = Field(frozen=True, description="Called with each event's name and payload")

    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="screen-neko-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._follow_once()
            except (WebSocketException, OSError, ValueError) as e:
                if self._stop.is_set():
                    return
                logger.debug("The neko event session dropped ({}); reconnecting", e)
            self._stop.wait(_EVENT_RECONNECT_SECONDS)

    def _follow_once(self) -> None:
        url = f"{self.websocket_url}?token={self.token.get_secret_value()}"
        with connect_websocket(url, open_timeout=_REQUEST_TIMEOUT_SECONDS) as connection:
            while not self._stop.is_set():
                try:
                    raw = connection.recv(timeout=_EVENT_RECEIVE_TIMEOUT_SECONDS)
                except TimeoutError:
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict) or not isinstance(message.get("event"), str):
                    continue
                payload = message.get("payload")
                self.handler(message["event"], payload if isinstance(payload, dict) else {})


def wait_until_healthy(
    neko: NekoInterface, timeout_seconds: float, poll_seconds: float, give_up: threading.Event
) -> bool:
    """Poll neko's health route until it answers, the deadline passes, or ``give_up`` is set."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not give_up.is_set():
        if neko.is_healthy():
            return True
        give_up.wait(poll_seconds)
    return neko.is_healthy()
