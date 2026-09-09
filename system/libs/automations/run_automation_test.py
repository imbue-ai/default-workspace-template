"""Tests for run_automation.sh's create: the argv it hands `mngr create` on an automation's first run.

The script is exercised as a real subprocess over a fake `uv` on PATH that records the
`mngr` argv it is asked to run and answers `mngr list` with no agents, so every run is the
first run and reaches the create.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent / "run_automation.sh"

_FAKE_UV = """#!/bin/sh
# `uv run mngr <verb> ...`: record a create, answer a list with nothing.
shift
shift
case "$1" in
  create) printf '%s\\n' "$@" >> "$RECORDED_CREATE" ;;
esac
exit 0
"""


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    # The script resolves the workspace label with python3.
    (bin_dir / "python3").symlink_to(sys.executable)
    recorded = tmp_path / "create.argv"
    result = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RECORDED_CREATE": str(recorded),
        },
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    argv = recorded.read_text().splitlines() if recorded.exists() else []
    return result, argv


def _values_of(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == flag]


def test_the_create_names_the_role_template_alone_and_no_harness(tmp_path: Path) -> None:
    """The harness and the account come from the workspace's create defaults, not from a template
    that stopped existing when harnesses moved to `--type`."""
    result, argv = _run(tmp_path, "news")

    assert result.returncode == 0, result.stderr
    assert argv[:2] == ["create", "news"]
    assert _values_of(argv, "--template") == ["automation"]
    assert "--type" not in argv
    assert _values_of(argv, "--label") == ["automation=news"]
    assert _values_of(argv, "--message") == ["/news"]


def test_a_type_override_rides_the_create_and_a_template_override_replaces_the_role(tmp_path: Path) -> None:
    result, argv = _run(tmp_path, "caretaker", "--template", "caretaker", "--type", "codex")

    assert result.returncode == 0, result.stderr
    assert _values_of(argv, "--template") == ["caretaker"]
    assert _values_of(argv, "--type") == ["codex"]
