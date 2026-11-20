import json
import os
import stat
import threading
from pathlib import Path

from imbue.chat.harnesses.grok.account_binding import API_KEY_SCOPE
from imbue.chat.harnesses.grok.account_binding import AUTH_FILENAME
from imbue.chat.harnesses.grok.auth import AUTH_LOCK_WAIT_SECONDS
from imbue.chat.harnesses.grok.auth import drop_api_key
from imbue.chat.harnesses.grok.auth import write_api_key
from imbue.chat.harnesses.grok.testing import GROK_SESSION_SCOPE
from imbue.chat.harnesses.grok.testing import build_grok_key_scope
from imbue.chat.harnesses.grok.testing import grok_holds_its_lock
from imbue.chat.harnesses.grok.testing import take_grok_lock
from imbue.chat.harnesses.grok.testing import write_grok_auth

_LOGIN = {"key": "access-token", "auth_mode": "oidc", "refresh_token": "refresh-token"}


def test_a_key_is_never_in_a_file_wider_than_owner_only(tmp_path: Path) -> None:
    """A wider mode on the auth.json the key replaces is not carried onto the file the key is written into."""
    auth_path = tmp_path / AUTH_FILENAME
    write_grok_auth(tmp_path, {GROK_SESSION_SCOPE: _LOGIN})
    auth_path.chmod(0o644)
    renamed: list[tuple[int, str]] = []

    def rename_noting_mode(source: str, destination: Path) -> None:
        renamed.append((stat.S_IMODE(os.stat(source).st_mode), Path(source).read_text()))
        os.replace(source, destination)

    assert write_api_key(auth_path, "xai-new-key", AUTH_LOCK_WAIT_SECONDS, rename_noting_mode)

    assert [(oct(mode), "xai-new-key" in text) for mode, text in renamed] == [("0o600", True)]
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    assert json.loads(auth_path.read_text())[API_KEY_SCOPE]["key"] == "xai-new-key"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["auth.json", "auth.json.lock"]


def test_a_key_waits_out_grok_s_own_write_to_the_file(tmp_path: Path) -> None:
    """grok holds its lock from reading the file to writing it back. A key that did not wait for it would be
    overwritten by what grok read before the key."""
    auth_path = tmp_path / AUTH_FILENAME
    write_grok_auth(tmp_path, {API_KEY_SCOPE: build_grok_key_scope("xai-old-key")})
    lock_fd = take_grok_lock(auth_path)
    read_by_grok = json.loads(auth_path.read_text())

    def grok_writes_back_and_unlocks() -> None:
        write_grok_auth(tmp_path, {**read_by_grok, GROK_SESSION_SCOPE: _LOGIN})
        os.close(lock_fd)

    grok_write = threading.Timer(0.5, grok_writes_back_and_unlocks)
    grok_write.start()
    is_written = write_api_key(auth_path, "xai-new-key", AUTH_LOCK_WAIT_SECONDS)
    grok_write.join()

    assert is_written
    after = json.loads(auth_path.read_text())
    assert list(after) == [API_KEY_SCOPE]
    assert after[API_KEY_SCOPE]["key"] == "xai-new-key"


def test_the_key_stays_beside_a_login_while_grok_holds_its_lock(tmp_path: Path) -> None:
    """As grok's own scope removal does: the login outranks the key anyway."""
    auth_path = tmp_path / AUTH_FILENAME
    write_grok_auth(tmp_path, {API_KEY_SCOPE: build_grok_key_scope(), GROK_SESSION_SCOPE: _LOGIN})
    written = auth_path.read_bytes()

    with grok_holds_its_lock(auth_path):
        drop_api_key(auth_path, 0.2)

    assert auth_path.read_bytes() == written


def test_a_file_grok_took_the_key_out_of_meanwhile_is_left_as_grok_wrote_it(tmp_path: Path) -> None:
    auth_path = tmp_path / AUTH_FILENAME
    write_grok_auth(tmp_path, {API_KEY_SCOPE: build_grok_key_scope(), GROK_SESSION_SCOPE: _LOGIN})
    lock_fd = take_grok_lock(auth_path)
    inode_by_grok: list[int] = []

    def grok_drops_the_key_and_unlocks() -> None:
        write_grok_auth(tmp_path, {GROK_SESSION_SCOPE: _LOGIN})
        inode_by_grok.append(auth_path.stat().st_ino)
        os.close(lock_fd)

    grok_write = threading.Timer(0.5, grok_drops_the_key_and_unlocks)
    grok_write.start()
    drop_api_key(auth_path, AUTH_LOCK_WAIT_SECONDS)
    grok_write.join()

    assert json.loads(auth_path.read_text()) == {GROK_SESSION_SCOPE: _LOGIN}
    assert [auth_path.stat().st_ino] == inode_by_grok
