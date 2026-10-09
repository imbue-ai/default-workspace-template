"""The staged entry script refuses a Python older than the 3.12 floor before importing anything."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_STUB = Path(__file__).resolve().parents[1] / "update_self.py"

# Runs the stub as __main__ under a Python that reports itself as 3.11, then records which
# of the skill's modules had been imported by the time it exited.
_AS_PYTHON_3_11 = """
import json, runpy, sys
sys.version_info = (3, 11, 9, "final", 0)
sys.argv = [sys.argv[1], "--help"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    loaded = sorted(name for name in sys.modules if name.startswith("update_self_skill"))
    sys.stdout.write(json.dumps(loaded))
"""


def test_a_python_below_the_floor_is_told_the_update_is_impossible() -> None:
    result = subprocess.run(
        [sys.executable, "-S", "-s", "-c", _AS_PYTHON_3_11, str(_STUB)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 75, result.stderr
    assert "cannot be updated in place" in result.stderr
    assert "migrate-workspace" in result.stderr
    assert result.stdout == "[]"


def test_the_stub_parses_as_python_3_11() -> None:
    ast.parse(_STUB.read_text(), feature_version=(3, 11))
