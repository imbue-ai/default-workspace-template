"""Everything this app asks of Imbue Studio, as HTTP calls.

The app shares no code with the workspace it runs in. What it needs from Studio it gets over HTTP: the workspace
shell (loopback) and the workspace's latchkey gateway. Every route used here is listed in
``docs/studio-contract.md``.
"""

import os
import threading
import urllib.error
import urllib.request
from typing import Any

from studio_discord.web import request_json

# The workspace shell. Studio's own code reads the same variable for the same address.
SHELL_URL = os.environ.get("MINDS_WORKSPACE_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")

APP_NAME = "studio-discord"
LAUNCH = "open"

# The browser modules every app page imports from its own origin: the app contract (how a page talks to the
# shell framing it) and the element context menu. The shell serves them at /_static/<name>, but a browser
# cannot import them cross-origin (the forwarder refuses cookieless cross-origin module fetches), so this app
# fetches them server-side and re-serves them.
SHELL_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# What the app asks Discord access for. Read-only; the app itself only reads the Studio channels.
DISCORD_SCOPE = "discord-api"
DISCORD_PERMISSIONS = ("discord-read-messages", "discord-read-channels", "discord-read-users")
_PERMISSIONS_THAT_READ_MESSAGES = frozenset({"discord-read-messages", "discord-read-all", "any"})
ACCESS_RATIONALE = (
    "Studio Discord: show the latest messages from the Imbue Studio channels (general, show-and-tell, "
    "help-and-bugs) inside Studio. Read-only. Discord's permission covers your whole Discord account; "
    "the app only ever reads those three channels."
)

_FETCH_TIMEOUT_SECONDS = 5.0


class ShellUnavailableError(Exception):
    """The shell could not be reached, or did not have what was asked for."""


_module_cache: dict[str, bytes] = {}
_module_cache_lock = threading.Lock()


def fetch_shell_module(name: str) -> bytes:
    """Return one of the shell's browser modules, fetched once and then kept for the life of the process.

    A module the shell changes reaches this app at its next restart, the same moment it reaches an in-tree app
    (whose copy is read from the shell's build output when the app starts serving).
    """
    if name not in SHELL_MODULE_NAMES:
        raise KeyError(name)
    with _module_cache_lock:
        cached = _module_cache.get(name)
    if cached is not None:
        return cached
    url = f"{SHELL_URL}/_static/{name}"
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ShellUnavailableError(f"GET {url} failed: {error}") from error
    with _module_cache_lock:
        _module_cache[name] = body
    return body


def clear_module_cache() -> None:
    with _module_cache_lock:
        _module_cache.clear()


# --- Discord access, through the workspace's latchkey gateway ---


def _gateway() -> str:
    return os.environ.get("LATCHKEY_GATEWAY", "").rstrip("/")


def _gateway_headers() -> dict[str, str]:
    headers = {"X-Latchkey-Gateway-Password": os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")}
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")
    if override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = override
    return headers


def discord_access_granted() -> bool:
    """Whether the workspace holds a Discord grant that can read messages (for any account)."""
    granted = request_json(f"{_gateway()}/permissions/self", headers=_gateway_headers())
    for rule in granted.get("rules", []):
        for scope, permissions in rule.items():
            if scope.split(":", 1)[0] == DISCORD_SCOPE and _PERMISSIONS_THAT_READ_MESSAGES & set(permissions):
                return True
    return False


def request_discord_access() -> str:
    """Ask the user for read access. The request appears in their Studio notifications; returns its id."""
    body = {
        "agent_id": os.environ.get("MNGR_AGENT_ID", ""),
        "type": "predefined",
        "payload": {"scope": DISCORD_SCOPE, "permissions": list(DISCORD_PERMISSIONS)},
        "rationale": ACCESS_RATIONALE,
    }
    created = request_json(f"{_gateway()}/permission-requests", method="POST", headers=_gateway_headers(), body=body)
    return str(created.get("request_id", ""))


# --- Desktop icons, through the shell ---


def _desktops() -> list[dict[str, Any]]:
    return request_json(f"{SHELL_URL}/api/desktops").get("desktops", [])


def _is_this_app(shortcut: dict[str, Any]) -> bool:
    target = shortcut.get("target", {})
    return target.get("kind") == "launch" and target.get("app") == APP_NAME


def remove_desktop_icons() -> list[dict[str, Any]]:
    """Take this app's icon off every desktop. Returns what was removed, so it can be put back exactly."""
    removed: list[dict[str, Any]] = []
    for desktop in _desktops():
        for shortcut in desktop.get("shortcuts", []):
            if _is_this_app(shortcut):
                request_json(
                    f"{SHELL_URL}/api/desktops/{desktop['id']}/shortcuts/remove",
                    method="POST",
                    body={"app": APP_NAME, "launch": shortcut["target"].get("launch", LAUNCH)},
                )
                removed.append({"desktop_id": desktop["id"], "shortcut": shortcut})
    return removed


def restore_desktop_icons(removed: list[dict[str, Any]]) -> None:
    """Put back icons ``remove_desktop_icons`` took off, at the same spot, on desktops that still exist."""
    existing = {desktop["id"] for desktop in _desktops()}
    for entry in removed:
        if entry["desktop_id"] not in existing:
            continue
        shortcut = entry["shortcut"]
        request_json(
            f"{SHELL_URL}/api/desktops/{entry['desktop_id']}/shortcuts",
            method="POST",
            body={"target": shortcut["target"], "mode": shortcut.get("mode", "focus"), "cell": shortcut["cell"]},
        )
