"""The early sign-in as the Imbue Studio app starts it, through its launcher: it names its port, serves, and ends on hang-up."""

import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

from imbue.chat.early_sign_in import STARTUP_LINE_PREFIX

_CHECKOUT = Path(__file__).resolve().parents[3]
_LAUNCHER_PATH_IN_CHECKOUT = Path("system") / "scripts" / "start_early_sign_in.sh"


@pytest.mark.timeout(60)
def test_the_program_names_its_port_serves_and_clears_its_staging_store_on_hang_up(tmp_path: Path) -> None:
    home = tmp_path / "home"
    leftover = home / ".minds-early-sign-in" / ".minds" / "accounts" / "0f3c9a"
    leftover.mkdir(parents=True)
    # The launcher starts the program from the root of the checkout it sits in. A scratch one, holding only
    # the launcher and this checkout's venv, keeps it from loading this checkout's mngr settings.
    checkout = tmp_path / "checkout"
    launcher = checkout / _LAUNCHER_PATH_IN_CHECKOUT
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(_CHECKOUT / _LAUNCHER_PATH_IN_CHECKOUT)
    (checkout / ".venv").symlink_to(_CHECKOUT / ".venv")
    env = dict(os.environ)
    env.update({"HOME": str(home), "MINDS_ACCOUNTS_ROOT": str(tmp_path / "live-accounts")})

    process = subprocess.Popen(
        ["bash", str(launcher)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        startup_line = process.stdout.readline()
        assert startup_line.startswith(STARTUP_LINE_PREFIX), startup_line
        port = json.loads(startup_line.removeprefix(STARTUP_LINE_PREFIX))["port"]
        is_staging_cleared_at_start = not leftover.exists()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/early-sign-in", timeout=10) as response:
            status = json.load(response)

        process.stdin.close()
        exit_code = process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)

    assert is_staging_cleared_at_start
    assert status == {"is_flow_pending": False, "is_done": False, "account_ids": []}
    assert exit_code == 0
    assert not (home / ".minds-early-sign-in").exists()
