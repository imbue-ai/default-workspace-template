"""The script paths other programs call keep working, under the interpreter those callers use.

Imbue Studio (minds), its evals and admin tools, the web client (through owner-exec), the
mngr create hook, CI and older releases' update-self flows all run these files by path, from
outside this release. A bare one runs with the system ``python3`` and no venv, so it is run here
as ``python -S -s`` (no site-packages at all); the one venv-tier path is run from the root venv,
as its callers' ``uv run --no-sync`` would.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from entry_points_testing import IMPORT_PROBE, REPO_ROOT

_SYSTEM_SCRIPTS = REPO_ROOT / "system" / "scripts"
_UPDATE_SELF_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "update-self" / "scripts"

# (path, arguments) for each bare path an outside caller runs; every one is harmless.
_BARE_CALLS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (_SYSTEM_SCRIPTS / "seed_welcome_chat.py", ("--help",)),
    (_SYSTEM_SCRIPTS / "message_chat.py", ("--help",)),
    (_SYSTEM_SCRIPTS / "forward_port.py", ("--help",)),
    (_SYSTEM_SCRIPTS / "provision_backups.py", ("--help",)),
    (_SYSTEM_SCRIPTS / "set_mngr_pin.py", ("--help",)),
    (_SYSTEM_SCRIPTS / "run_in_background.py", ("--help",)),
    # Outside a workspace (no agent id in the environment) the gate lets everything through.
    (_SYSTEM_SCRIPTS / "require_create_account.py", ()),
    (_UPDATE_SELF_SCRIPTS / "update_self.py", ("--help",)),
    (_UPDATE_SELF_SCRIPTS / "run_in_background.py", ("--help",)),
)

# Imported without letting the __main__ block act (a run would collect a real bug report), which
# still resolves every import a real run makes at startup.
_BARE_IMPORTS = (_SYSTEM_SCRIPTS / "collect_bug_report_diagnostics.py",)

_VENV_CALLS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (REPO_ROOT / ".agents" / "skills" / "launch-task" / "scripts" / "create_worker.py", ("--help",)),
)


def _bare_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("script", "arguments"), _BARE_CALLS, ids=lambda value: str(value).rsplit("/", 2)[-1]
)
def test_a_bare_external_path_runs_without_the_venv(script: Path, arguments: tuple[str, ...]) -> None:
    result = _run([sys.executable, "-S", "-s", str(script), *arguments], REPO_ROOT, _bare_environment())

    assert result.returncode == 0, result.stderr


@pytest.mark.timeout(60)
@pytest.mark.parametrize("script", _BARE_IMPORTS, ids=lambda path: path.name)
def test_a_bare_external_path_imports_without_the_venv(script: Path) -> None:
    result = _run(
        [sys.executable, "-S", "-s", "-c", IMPORT_PROBE, str(script)], REPO_ROOT, _bare_environment()
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.timeout(60)
def test_the_secrets_wrapper_runs_a_command_without_the_venv(tmp_path: Path) -> None:
    secrets = tmp_path / "data" / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "probe.env").write_text("PROBE_VALUE=1\n")
    (secrets / "probe.env").chmod(0o600)

    result = _run(
        [
            sys.executable,
            "-S",
            "-s",
            str(_SYSTEM_SCRIPTS / "with_secrets.py"),
            "data/.secrets/probe.env",
            "--",
            "sh",
            "-c",
            'test "$PROBE_VALUE" = 1',
        ],
        tmp_path,
        _bare_environment(),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.timeout(60)
@pytest.mark.parametrize(("script", "arguments"), _VENV_CALLS, ids=lambda value: str(value).rsplit("/", 2)[-1])
def test_a_venv_external_path_runs_from_the_root_venv(script: Path, arguments: tuple[str, ...]) -> None:
    result = _run([sys.executable, str(script), *arguments], REPO_ROOT, dict(os.environ))

    assert result.returncode == 0, result.stderr


@pytest.mark.timeout(60)
def test_a_bare_path_that_reaches_for_the_venv_fails_here(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    (scripts / "probe_pkg").mkdir(parents=True)
    (scripts / "probe_pkg" / "__init__.py").write_text("")
    (scripts / "probe_pkg" / "cli.py").write_text("import yaml\n\n\ndef main() -> None:\n    pass\n")
    stub = scripts / "probe.py"
    stub.write_text('from probe_pkg.cli import main\n\nif __name__ == "__main__":\n    main()\n')

    result = _run([sys.executable, "-S", "-s", str(stub), "--help"], tmp_path, _bare_environment())

    assert result.returncode != 0
    assert "No module named 'yaml'" in result.stderr
