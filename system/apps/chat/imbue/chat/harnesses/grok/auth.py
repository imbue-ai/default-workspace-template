"""grok's auth.json as the chat writes it: a pasted key's scope, written and dropped under grok's own lock."""

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.grok.account_binding import API_KEY_SCOPE
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict
from imbue.mngr.utils.polling import poll_until

logger = _loguru_logger

# grok's advisory lock beside its auth.json, which its own writers hold across each read and write.
_AUTH_LOCK_FILENAME: Final = "auth.json.lock"
# How long grok itself waits for that lock (`AUTH_LOCK_TIMEOUT`).
AUTH_LOCK_WAIT_SECONDS: Final = 10.0
_AUTH_LOCK_POLL_SECONDS: Final = 0.05


def _try_exclusive_lock(lock_fd: int) -> bool:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def _auth_file_locked(auth_path: Path, wait_seconds: float) -> Generator[bool, None, None]:
    """Hold grok's own lock on its auth.json for the block, yielding whether it was taken in time.

    Every agent on the account rewrites the shared file whole from what it read under this lock, and the refresh
    token in it is single-use: an unlocked write can be lost under grok's, or put back a token grok has spent.
    """
    lock_fd = os.open(auth_path.with_name(_AUTH_LOCK_FILENAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        yield poll_until(lambda: _try_exclusive_lock(lock_fd), wait_seconds, _AUTH_LOCK_POLL_SECONDS)
    finally:
        # Closing releases the lock; the file stays, as grok's own lock file does.
        os.close(lock_fd)


def _replace_owner_only(path: Path, text: str) -> None:
    """Rename into ``path`` a sibling temp file that holds ``text`` and is 0600 from its creation."""
    temp_fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def write_api_key(auth_path: Path, api_key: str, lock_wait_seconds: float) -> bool:
    """Make grok's auth.json hold the key alone, in its `xai::api_key` scope, or write nothing and say False.

    A session login left beside the key would outrank it. The entry is the one grok's own `store_api_key` writes,
    which grok 1.0.62 reads back as its key. False means grok held its lock past ``lock_wait_seconds``.
    """
    with _auth_file_locked(auth_path, lock_wait_seconds) as is_locked:
        if not is_locked:
            return False
        scope = {
            "key": api_key,
            "auth_mode": "api_key",
            "create_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "user_id": "",
            "email": None,
            "coding_data_retention_opt_out": True,
        }
        _replace_owner_only(auth_path, json.dumps({API_KEY_SCOPE: scope}, indent=2) + "\n")
        return True


def _is_api_key_beside_another_scope(scopes: dict[str, Any]) -> bool:
    return API_KEY_SCOPE in scopes and len(scopes) > 1


def drop_api_key(auth_path: Path, lock_wait_seconds: float) -> None:
    """Remove the `xai::api_key` scope from grok's auth.json when another scope sits beside it, keeping those.

    `grok login` leaves a stored key beside the login it adds, and grok turns to that key whenever the
    login is not usable, so a device sign-in takes it away. A key that is the file's only credential stays.
    """
    if not _is_api_key_beside_another_scope(read_json_dict(auth_path)):
        return
    with _auth_file_locked(auth_path, lock_wait_seconds) as is_locked:
        if not is_locked:
            # As grok's own scope removal does when its lock is held: the login still outranks the key.
            logger.warning("Left the {} scope in {}: grok held its lock", API_KEY_SCOPE, auth_path)
            return
        scopes = read_json_dict(auth_path)
        if not _is_api_key_beside_another_scope(scopes):
            return
        del scopes[API_KEY_SCOPE]
        atomic_write(auth_path, json.dumps(scopes, indent=2) + "\n")
