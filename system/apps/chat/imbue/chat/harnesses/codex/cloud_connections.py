"""Codex's cloud connections: the apps (connectors) of a ChatGPT account.

A codex signed in with ChatGPT loads the apps connected on that account through one MCP server
OpenAI hosts (``codex_apps``), unless ``features.apps`` is off in its config. The workspace turns
it off in every agent's ``config.toml`` (``.mngr/settings.toml``), so an agent loads them only
once its own config says ``apps = true``; codex reads that when the agent's thread is loaded.

The list is asked of the account's own codex, the way its models are
(:mod:`~imbue.chat.harnesses.codex.account_models`): a short-lived ``codex app-server`` bound to
the account folder, with apps on for that one daemon, answers ``app/list``. An account signed in
with an API key has no apps, and its codex answers with an empty list.
"""

import json
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from typing import Final

import tomlkit
from loguru import logger as _loguru_logger
from tomlkit.exceptions import ParseError

from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.codex.account_models import AccountModelProbeError
from imbue.chat.harnesses.codex.account_models import ask_codex_account
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home

logger = _loguru_logger

_FEATURES_TABLE: Final = "features"
_APPS_FEATURE: Final = "apps"
_APPS_ON_OVERRIDE: Final = f"{_FEATURES_TABLE}.{_APPS_FEATURE}=true"

_APP_LIST_METHOD: Final = "app/list"
# A string, so it cannot collide with the integer ids the client numbers its own requests with.
_APP_LIST_REQUEST_ID: Final = "cloud-connections-app-list"
# The list is the whole directory with the account's connected apps sorted first, so one page of
# this size holds every connected app of any plausible account.
_APP_LIST_PAGE_SIZE: Final = 200
# codex itself gives up loading the list after 90 seconds.
_APP_LIST_TIMEOUT_SECONDS: Final = 120.0
_DAEMON_TIMEOUT_SECONDS: Final = 150.0
_SOCKET_FILENAME: Final = "app-server.sock"
_SOCKET_PARENT_DIR: Final = "/tmp"


def _connected_app_names(result: Any) -> tuple[str, ...]:
    """The names of the apps an ``app/list`` result marks as connected on the account."""
    entries = result.get("data") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        raise CodexAppServerError(f"codex app-server answered {_APP_LIST_METHOD!r} with no list of apps")
    names = [
        entry["name"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("isAccessible") is True
        and isinstance(entry.get("name"), str)
        and entry["name"]
    ]
    return tuple(dict.fromkeys(names))


def _parse_frame(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring a non-JSON frame from codex app-server while listing apps: {}", raw[:200])
        return None


def list_connected_apps(client: CodexAppServerClient) -> tuple[str, ...]:
    """Ask an initialized app-server for the account's apps, reading frames until it answers.

    The answer arrives after however many ``app/list/updated`` notifications codex sends while it loads.
    """
    transport = client.transport
    request = {
        "jsonrpc": "2.0",
        "id": _APP_LIST_REQUEST_ID,
        "method": _APP_LIST_METHOD,
        "params": {"limit": _APP_LIST_PAGE_SIZE, "forceRefetch": True},
    }
    transport.send(json.dumps(request))
    deadline = time.monotonic() + _APP_LIST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            raw = transport.receive(max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            break
        frame = _parse_frame(raw)
        if not isinstance(frame, dict) or frame.get("id") != _APP_LIST_REQUEST_ID:
            continue
        error = frame.get("error")
        if error is not None:
            raise CodexAppServerError(f"codex app-server request {_APP_LIST_METHOD!r} failed: {error!r}")
        return _connected_app_names(frame.get("result"))
    raise CodexAppServerError(f"codex app-server request {_APP_LIST_METHOD!r} timed out")


def _read_config(config_path: Path) -> tomlkit.TOMLDocument:
    try:
        text = config_path.read_text()
    except OSError as e:
        raise CloudConnectionsError(f"the codex config at {config_path} is unreadable: {e}") from e
    try:
        return tomlkit.parse(text)
    except ParseError as e:
        raise CloudConnectionsError(f"the codex config at {config_path} is not TOML: {e}") from e


class CodexCloudConnections(CloudConnections):
    """The apps of the ChatGPT account, and the ``features.apps`` flag that makes one agent load them."""

    def list_connected(self, account_dir: Path) -> tuple[str, ...]:
        # A socket of its own rather than the account's: the account's is taken, under its probe
        # lock, by whoever is asking for the account's models, and this exchange can run for minutes.
        # Under /tmp whatever TMPDIR says, as mngr's own codex sockets are: a unix socket's path is
        # limited to about a hundred bytes.
        with tempfile.TemporaryDirectory(prefix="minds-codex-apps-", dir=_SOCKET_PARENT_DIR) as socket_dir:
            try:
                return ask_codex_account(
                    account_dir,
                    Path(socket_dir) / _SOCKET_FILENAME,
                    list_connected_apps,
                    config_overrides=(_APPS_ON_OVERRIDE,),
                    timeout_seconds=_DAEMON_TIMEOUT_SECONDS,
                )
            except AccountModelProbeError as e:
                raise CloudConnectionsError(str(e)) from e

    def is_enabled(self, agent_state_dir: Path) -> bool:
        features = _read_config(get_codex_config_path(get_codex_home(agent_state_dir))).get(_FEATURES_TABLE)
        # codex loads the apps unless its config turns them off.
        return not isinstance(features, MutableMapping) or features.get(_APPS_FEATURE) is not False

    def enable(self, agent_state_dir: Path) -> None:
        config_path = get_codex_config_path(get_codex_home(agent_state_dir))
        document = _read_config(config_path)
        features = document.get(_FEATURES_TABLE)
        if features is None:
            features = tomlkit.table()
            document[_FEATURES_TABLE] = features
        if not isinstance(features, MutableMapping):
            raise CloudConnectionsError(
                f"the codex config at {config_path} has a [{_FEATURES_TABLE}] that is no table"
            )
        features[_APPS_FEATURE] = True
        try:
            atomic_write(config_path, tomlkit.dumps(document))
        except OSError as e:
            raise CloudConnectionsError(f"the codex config at {config_path} could not be written: {e}") from e
