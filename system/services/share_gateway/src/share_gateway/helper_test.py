"""The served helper's own test suite (Node's test runner over the vendored owner-exec vectors), run from pytest."""

import subprocess
from pathlib import Path

from share_gateway.agent_orientation import HELPER_PATH

_HELPER_DIR = Path(__file__).parent / "helper"


def test_helper_suite_passes_under_node() -> None:
    result = subprocess.run(
        ["node", "--test", str(_HELPER_DIR / "wsvisit_test.cjs")],
        cwd=_HELPER_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")


def test_helper_is_the_file_the_gateway_serves_and_prints_usage() -> None:
    assert HELPER_PATH == _HELPER_DIR / "wsvisit.cjs"
    result = subprocess.run(
        ["node", str(HELPER_PATH), "help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False
    )
    assert result.returncode == 0
    usage = result.stdout.decode("utf-8")
    assert "wsvisit join <auth-origin> <code>" in usage and "wsvisit run" in usage
    # The whole header comment, to its last sentence, not a line count that drifts.
    assert usage.rstrip().endswith("retrying once.")
