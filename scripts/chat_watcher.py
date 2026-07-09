"""One-shot: deliver a test case's first prompt to this workspace's chat agent as a user
message -- the exact call the Minds chat box makes -- once the agent finishes its opening
turn and is idle (state WAITING).

Reads the slotted-in test-case data file ``scripts/first_command.json``
(``{"id", "persona", "first_prompt"}``). Absent -> no-op (normal, non-eval workspaces).
Runs under supervisord as a one-shot; a marker file guards against re-posting on a container
restart. No LLM, no external controller: everything is loopback to the local system_interface
at 127.0.0.1:8000, which is unauthenticated from inside the sandbox.

This intentionally does NOT parse the /welcome text; it gates on the generic agent state so it
never races a mid-turn agent, and so the same wait->send loop can be extended to multi-turn later.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_SYSTEM_INTERFACE = "http://127.0.0.1:8000"
# Paths are relative to /mngr/code (the cwd supervisord runs services in).
_CONFIG_PATH = Path("scripts/first_command.json")
_MARKER_PATH = Path("runtime/first_command_sent")
_CHAT_AGENT_ID_FILENAME = "initial_chat_agent_id"
_OVERALL_TIMEOUT_SECONDS = 900.0
_POLL_INTERVAL_SECONDS = 3.0


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_message(agent_id: str, message: str) -> int:
    body = json.dumps({"message": message}).encode("utf-8")
    request = urllib.request.Request(
        "{}/api/agents/{}/message".format(_SYSTEM_INTERFACE, agent_id),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def _read_first_prompt() -> str | None:
    if not _CONFIG_PATH.is_file():
        return None
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except (ValueError, OSError):
        return None
    prompt = str(data.get("first_prompt", "")).strip()
    return prompt or None


def _resolve_chat_agent_id(deadline: float) -> str | None:
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    id_path = Path(host_dir) / _CHAT_AGENT_ID_FILENAME if host_dir else None
    while time.time() < deadline:
        if id_path is not None and id_path.is_file():
            agent_id = id_path.read_text().strip()
            if agent_id:
                return agent_id
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _agent_state(agent_id: str) -> str | None:
    try:
        agents = _get_json("{}/api/agents".format(_SYSTEM_INTERFACE)).get("agents", [])
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for agent in agents:
        if agent.get("id") == agent_id:
            return (agent.get("state") or "").upper()
    return None


def _wait_for_idle(agent_id: str, deadline: float) -> bool:
    while time.time() < deadline:
        if _agent_state(agent_id) == "WAITING":
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _send_with_retry(agent_id: str, message: str, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            if _post_message(agent_id, message) == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def main() -> None:
    prompt = _read_first_prompt()
    if prompt is None:
        print("[chat-watcher] no first_command.json / first_prompt -- nothing to do")
        return
    if _MARKER_PATH.exists():
        print("[chat-watcher] first prompt already sent (marker present) -- skipping")
        return

    deadline = time.time() + _OVERALL_TIMEOUT_SECONDS
    agent_id = _resolve_chat_agent_id(deadline)
    if agent_id is None:
        print("[chat-watcher] could not resolve chat agent id within timeout -- exiting")
        return
    if not _wait_for_idle(agent_id, deadline):
        print("[chat-watcher] agent {} never reached WAITING within timeout -- exiting".format(agent_id))
        return
    if _send_with_retry(agent_id, prompt, deadline):
        _MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MARKER_PATH.write_text("")
        print("[chat-watcher] delivered first prompt to {}".format(agent_id))
    else:
        print("[chat-watcher] failed to deliver first prompt to {} within timeout".format(agent_id))


if __name__ == "__main__":
    main()
