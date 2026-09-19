"""The secret-request flow end to end through the real router: the request script files a
request against a running chat app, the card's submit writes the file, and the chat's agent
receives a notice that names the file and the variables and never the value."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from werkzeug.serving import make_server

from imbue.chat.agent_manager import AgentManager
from imbue.chat.secret_requests import SecretRequestStore
from imbue.chat.server import create_application
from imbue.chat.testing import RecordingMngrMessenger
from imbue.chat.testing import build_test_state
from imbue.chat.testing import seed_agent_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster

_REQUEST_SCRIPT = (
    Path(__file__).resolve().parents[5]
    / ".agents"
    / "skills"
    / "connect-external-service"
    / "scripts"
    / "request_secret.py"
)
_AGENT_ID = "agent-00000000000000000000000000000031"


@contextmanager
def _running_chat_app(tmp_path: Path, messenger: RecordingMngrMessenger) -> Iterator[tuple[str, SecretRequestStore]]:
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    seed_agent_state(manager, _AGENT_ID, name="chat-1")
    manager.note_agent_list_known()
    store = SecretRequestStore(
        requests_directory=tmp_path / "data" / ".state" / "secret-requests",
        secrets_directory=tmp_path / "data" / ".secrets",
    )
    application = create_application(build_test_state(agent_manager=manager, secret_requests=store))
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", store
    finally:
        server.shutdown()
        thread.join(timeout=10)


def _run_request_script(tmp_path: Path, base_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text(f'[[apps]]\nname = "chat"\nurl = "{base_url}"\n')
    environment = {**os.environ, "MINDS_APPS_FILE": str(apps_file), "MINDS_CHAT_ID": _AGENT_ID}
    return subprocess.run(
        [sys.executable, str(_REQUEST_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
        env=environment,
    )


def test_a_request_filed_by_the_script_is_answered_into_the_file_and_the_chat(tmp_path: Path) -> None:
    messenger = RecordingMngrMessenger()
    value = "sk-live-" + "7" * 32
    with _running_chat_app(tmp_path, messenger) as (base_url, store):
        completed = _run_request_script(
            tmp_path, base_url, "--file", "svc", "--var", "SVC_TOKEN", "--rationale", "to call the widget API"
        )
        assert completed.returncode == 0, completed.stderr
        filed = json.loads(completed.stdout)
        assert filed["file"] == "svc" and filed["variables"] == ["SVC_TOKEN"] and filed["status"] == "pending"

        response = httpx.post(
            f"{base_url}/api/secret-requests/{filed['request_id']}/submit",
            json={"values": {"SVC_TOKEN": value}},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_notice_delivered"] is True

    env_file = tmp_path / "data" / ".secrets" / "svc.env"
    assert env_file.read_text() == f"SVC_TOKEN='{value}'\n"
    [(agent_id, notice)] = messenger.sent
    assert agent_id == _AGENT_ID
    assert (
        notice
        == f"Secret stored: data/.secrets/svc.env (SVC_TOKEN) (secret: stored, request_id: {filed['request_id']})"
    )
    # The value reached the file and nothing else the chat app keeps.
    record = (tmp_path / "data" / ".state" / "secret-requests" / f"{filed['request_id']}.json").read_text()
    assert value not in record
    assert value not in completed.stdout


def test_the_script_fails_plainly_when_no_chat_app_answers(tmp_path: Path) -> None:
    completed = _run_request_script(
        tmp_path, "http://127.0.0.1:9", "--file", "svc", "--var", "SVC_TOKEN", "--rationale", "why"
    )
    assert completed.returncode == 1
    assert "cannot be reached" in completed.stderr or "could not connect" in completed.stderr
    assert "from a terminal" in completed.stderr


def test_the_script_refuses_a_bad_file_or_variable_name_before_reaching_the_chat_app(tmp_path: Path) -> None:
    completed = _run_request_script(
        tmp_path, "http://127.0.0.1:9", "--file", "Svc", "--var", "TOKEN", "--rationale", "why"
    )
    assert completed.returncode == 2
    assert "--file" in completed.stderr
    completed = _run_request_script(
        tmp_path, "http://127.0.0.1:9", "--file", "svc", "--var", "1TOKEN", "--rationale", "why"
    )
    assert completed.returncode == 2
    assert "--var" in completed.stderr
