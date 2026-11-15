import threading
from uuid import uuid4

from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from screen.data_types import NekoSession
from screen.data_types import NekoSessionRecord
from screen.data_types import ScreenSize
from screen.errors import NekoAuthenticationError
from screen.errors import NekoError
from screen.interfaces import NekoInterface
from screen.primitives import DisplayName
from screen.primitives import NekoSessionId
from screen.primitives import NekoToken

# A 1x1 white JPEG, so the PNG conversion has a real image to decode.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


class FakeNeko(NekoInterface):
    """neko in memory: logins mint sessions, the lease projection and host changes are recorded."""

    # The stack generates its passwords, which this fake never sees, so a login is classified by its name: the
    # service's own session is the admin one.
    admin_names: set[str] = Field(default_factory=lambda: {"Screen service"}, description="Names logged in as admins")
    is_refusing_logins: bool = Field(default=False, description="Whether every login is refused")
    is_up: bool = Field(default=True, description="Whether the health route answers")
    api_token: SecretStr | None = Field(default=None, description="What the stack handed over")
    is_locked: bool = Field(default=False, description="neko's locked_controls")
    host_id: str | None = Field(default=None, description="neko's host")
    size: ScreenSize = Field(default=ScreenSize(width=1920, height=1080, rate=60), description="The display mode")
    sessions: dict[str, NekoSessionRecord] = Field(default_factory=dict, description="Live sessions by id")
    calls: list[str] = Field(default_factory=list, description="Every call, in order")
    screenshot_bytes: bytes = Field(default=TINY_JPEG, description="What a screenshot answers")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def set_api_token(self, token: SecretStr | None) -> None:
        self.api_token = token

    def _require_token(self) -> None:
        if self.api_token is None:
            raise NekoError("no API token")

    def is_healthy(self) -> bool:
        return self.is_up

    def login(self, username: str, password: str, is_owner: bool) -> NekoSession:
        with self._lock:
            self.calls.append(f"login:{username}")
            if self.is_refusing_logins or not password:
                raise NekoAuthenticationError("bad password")
            is_admin = username in self.admin_names
            session_id = f"{username.lower().replace(' ', '-')}-{uuid4().hex[:5]}"
            self.sessions[session_id] = NekoSessionRecord(
                session_id=NekoSessionId(session_id),
                name=username,
                is_admin=is_admin,
                is_connected=True,
                not_connected_since=None,
            )
            return NekoSession(
                session_id=NekoSessionId(session_id),
                token=NekoToken(f"tok-{uuid4().hex}"),
                name=DisplayName(username),
                is_owner=is_owner,
                is_admin=is_admin,
            )

    def logout(self, token: NekoToken) -> None:
        self.calls.append("logout")

    def set_locked_controls(self, is_locked: bool) -> None:
        self._require_token()
        with self._lock:
            self.calls.append(f"lock:{is_locked}")
            self.is_locked = is_locked
            if is_locked:
                self.host_id = None

    def give_control(self, session_id: NekoSessionId) -> None:
        self._require_token()
        with self._lock:
            self.calls.append(f"give:{session_id}")
            self.host_id = str(session_id)

    def reset_control(self) -> None:
        self._require_token()
        with self._lock:
            self.calls.append("reset")
            self.host_id = None

    def screenshot_jpeg(self, quality: int) -> bytes:
        self._require_token()
        self.calls.append(f"shot:{quality}")
        return self.screenshot_bytes

    def screen_size(self) -> ScreenSize:
        self._require_token()
        return self.size

    def set_screen_size(self, size: ScreenSize) -> ScreenSize:
        self._require_token()
        with self._lock:
            self.calls.append(f"size:{size.width}x{size.height}")
            self.size = size
        return size

    def list_sessions(self) -> list[NekoSessionRecord]:
        self._require_token()
        with self._lock:
            return list(self.sessions.values())

    def delete_session(self, session_id: NekoSessionId) -> None:
        self._require_token()
        with self._lock:
            self.calls.append(f"delete:{session_id}")
            self.sessions.pop(str(session_id), None)
