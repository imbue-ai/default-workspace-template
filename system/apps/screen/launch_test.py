import os
from pathlib import Path

import pytest

from oom_priority import bands
from screen.launch import DetachedProcessLauncher
from screen.launch import screen_process_environment
from screen.neko_config import PULSE_SOCKET_PATH
from screen.primitives import DISPLAY
from testing import wait_until


def test_a_launched_command_runs_detached_with_the_screens_environment_in_the_agent_subprocess_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw-3f9c1e7a")
    report = tmp_path / "report"
    launcher = DetachedProcessLauncher(working_directory=tmp_path)
    pid = launcher.launch(
        [
            "sh",
            "-c",
            'printf "%s\n%s\n%s\n%s\n%s\n" "$PWD" "$DISPLAY" "$PULSE_SERVER" "${LATCHKEY_GATEWAY_PASSWORD:-absent}" "$(cat /proc/self/oom_score_adj 2>/dev/null)" > "$1"',
            "report-writer",
            str(report),
        ]
    )
    assert pid > 0
    assert wait_until(lambda: report.is_file() and report.read_text().count("\n") >= 5)
    cwd, display, pulse_server, gateway_password, band = report.read_text().splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert display == DISPLAY
    assert pulse_server == f"unix:{PULSE_SOCKET_PATH}"
    assert gateway_password == "absent"
    # The band is observable only where /proc is writable (Linux); the shell prefix is a no-op elsewhere.
    if os.access("/proc/self/oom_score_adj", os.W_OK):
        assert band == str(bands.AGENT_SUBPROCESS)


def test_the_screens_environment_withholds_the_gateway_credentials_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.test:7781")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw-8b2d4c6e")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "jwt-5a1f9d3b")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-7c3e2b1d")
    environment = screen_process_environment()
    assert "LATCHKEY_GATEWAY_PASSWORD" not in environment
    assert "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" not in environment
    assert environment["LATCHKEY_GATEWAY"] == "http://gateway.test:7781"
    assert environment["MNGR_AGENT_ID"] == "agent-7c3e2b1d"
    assert environment["DISPLAY"] == DISPLAY
    assert environment["PULSE_SERVER"] == f"unix:{PULSE_SOCKET_PATH}"
