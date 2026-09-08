"""System tests: the chat app over a real ``mngr observe``, the way a workspace runs them.

The observer is the ``agent-observer`` supervisord program in a workspace; here it is the
same ``mngr observe --quiet`` spawned against an isolated host dir, so what is under test is
the real writer, the real event file, and the chat's real follower of it.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from imbue.chat.agent_manager import AgentManager
from imbue.chat.server import create_application
from imbue.chat.testing import RecordingMngrMessenger
from imbue.chat.testing import build_test_state
from imbue.chat.testing import prepare_isolated_mngr_host_dir
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.utils.polling import wait_for


def _agent_events(client: FlaskClient) -> dict[str, Any]:
    return client.get("/api/health").get_json()["agent_events"]


@contextmanager
def _running_observer(host_dir: Path, work_dir: Path, log_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Run ``mngr observe --quiet`` over ``host_dir`` from an empty work dir until the block ends.

    The work dir carries no project-local mngr settings, so the observer reads only the
    isolated host dir's profile; its stderr goes to ``log_path`` so a failure can say why.
    """
    work_dir.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNGR_AGENT_")}
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            ["mngr", "observe", "--quiet"], cwd=work_dir, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


@pytest.mark.timeout(180)
def test_chat_lists_what_the_real_observer_reports_and_rides_out_its_restart(tmp_path: Path) -> None:
    """The chat boots before its observer, lists agents once the observer's opening snapshot lands,
    keeps its list through the observer's death, and recovers when a new observer takes over.

    An isolated host dir has no agents, so the list is empty; what the test pins is the
    instances API's readiness and the health field's account of the stream, over the real
    ``mngr observe`` rather than a held lock.
    """
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")
    host_dir = Path(os.environ["MNGR_HOST_DIR"])
    prepare_isolated_mngr_host_dir(host_dir)
    observer_log = tmp_path / "observer.log"

    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    state = build_test_state(agent_manager=manager)
    client = create_application(state).test_client()
    try:
        manager._start_follow()
        assert client.get("/_instances").status_code == 503
        wait_for(lambda: "holds the lock" in _agent_events(client)["detail"], timeout=10.0)

        with _running_observer(host_dir, tmp_path / "work", observer_log):
            wait_for(
                lambda: _agent_events(client)["is_stream_healthy"],
                timeout=90.0,
                error_message=f"the chat never folded the observer's opening snapshot; see {observer_log}",
            )
            listed = client.get("/_instances")
            assert listed.status_code == 200
            assert listed.get_json() == {"instances": []}

        # The observer is gone. Nothing changes under the follower's directory watch, so the
        # outage is noticed by the fallback poll (ten seconds); the list stays served meanwhile.
        wait_for(lambda: "exited" in _agent_events(client)["detail"], timeout=30.0)
        assert not _agent_events(client)["is_stream_healthy"]
        assert client.get("/_instances").status_code == 200

        with _running_observer(host_dir, tmp_path / "work", observer_log):
            wait_for(
                lambda: _agent_events(client)["is_stream_healthy"],
                timeout=90.0,
                error_message=f"the chat never picked the restarted observer up; see {observer_log}",
            )
    finally:
        state.shutdown()
