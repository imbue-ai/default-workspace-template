"""The update apply's throwaway boot: ``chat-app --preflight`` serves its health route and touches nothing live."""

import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app_instances.testing import free_port

from imbue.mngr.utils.polling import wait_for


def _is_serving(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=0.5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def test_a_preflight_boot_serves_health_without_running_mngr_or_registering(tmp_path: Path) -> None:
    # A recording ``mngr``: the pre-flight must never reach it (no observe, no discovery).
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mngr_log = tmp_path / "mngr-calls.log"
    fake_mngr = fake_bin / "mngr"
    fake_mngr.write_text(f'#!/bin/sh\necho "$@" >> "{mngr_log}"\nexit 0\n')
    fake_mngr.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "CHAT_HOST": "127.0.0.1",
            "CHAT_PORT": str(port),
            "MNGR_HOST_DIR": str(tmp_path / "host"),
            "MNGR_AGENT_WORK_DIR": str(workspace),
            "MINDS_ACCOUNTS_ROOT": str(tmp_path / "accounts"),
        }
    )
    env.pop("MNGR_AGENT_ID", None)
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(
        [sys.executable, "-m", "imbue.chat.main", "--preflight"],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for(
            lambda: _is_serving(base_url) or process.poll() is not None,
            timeout=30.0,
            error_message=f"the pre-flight boot did not answer on {base_url}",
        )
        assert process.poll() is None, "the pre-flight boot exited before serving"
        assert _is_serving(base_url)
    finally:
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)

    assert not mngr_log.exists(), f"the pre-flight boot ran mngr: {mngr_log.read_text()}"
    # No registration: the registry lives under data/.state of the working directory.
    assert not (workspace / "data").exists()
    assert "pre-flight mode" in output
