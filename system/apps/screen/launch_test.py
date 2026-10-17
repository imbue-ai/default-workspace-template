import os
from pathlib import Path

from oom_priority import bands
from screen.launch import DetachedProcessLauncher
from screen.neko_config import PULSE_SOCKET_PATH
from screen.primitives import DISPLAY
from testing import wait_until


def test_a_launched_command_runs_detached_with_the_screens_environment_in_the_agent_subprocess_band(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report"
    launcher = DetachedProcessLauncher(working_directory=tmp_path)
    pid = launcher.launch(
        [
            "sh",
            "-c",
            'printf "%s\n%s\n%s\n%s\n" "$PWD" "$DISPLAY" "$PULSE_SERVER" "$(cat /proc/self/oom_score_adj 2>/dev/null)" > "$1"',
            "report-writer",
            str(report),
        ]
    )
    assert pid > 0
    assert wait_until(lambda: report.is_file() and report.read_text().count("\n") >= 4)
    cwd, display, pulse_server, band = report.read_text().splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert display == DISPLAY
    assert pulse_server == f"unix:{PULSE_SOCKET_PATH}"
    # The band is observable only where /proc is writable (Linux); the shell prefix is a no-op elsewhere.
    if os.access("/proc/self/oom_score_adj", os.W_OK):
        assert band == str(bands.AGENT_SUBPROCESS)
