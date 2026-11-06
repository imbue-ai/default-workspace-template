"""``chat-early-sign-in`` as the Imbue Studio app runs it: it names its port, serves, and ends on hang-up."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from imbue.chat.early_sign_in import STARTUP_LINE_PREFIX


@pytest.mark.timeout(60)
def test_the_program_names_its_port_serves_and_clears_its_staging_store_on_hang_up(tmp_path: Path) -> None:
    home = tmp_path / "home"
    leftover = home / ".minds-early-sign-in" / ".minds" / "accounts" / "0f3c9a"
    leftover.mkdir(parents=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "MINDS_ACCOUNTS_ROOT": str(tmp_path / "live-accounts")})

    process = subprocess.Popen(
        [sys.executable, "-m", "imbue.chat.early_sign_in"],
        # Outside this checkout, whose mngr settings a process under pytest may not load.
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
