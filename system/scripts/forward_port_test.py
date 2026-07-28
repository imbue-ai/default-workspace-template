"""Tests for the app-port registry script.

The script upserts / removes entries in an apps.toml registry and validates
that the app name can serve as a service origin hostname label
(``<name>.agent-<hex>.localhost`` locally, ``<name>--<host>--<user>.<domain>``
on shares). We drive it end to end as a subprocess -- the way supervisord and
services invoke it -- with ``MINDS_APPS_FILE`` pointed at a sandboxed registry.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "forward_port.py"


def _run(script_args: list[str], apps_file: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MINDS_APPS_FILE": str(apps_file)}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *script_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _read_apps(apps_file: Path) -> list[dict[str, str]]:
    return tomllib.loads(apps_file.read_text()).get("apps", [])


@pytest.mark.parametrize("name", ["terminal", "browser", "my-app", "app2", "a", "openvscode-server-4"])
def test_valid_names_register_and_are_persisted(tmp_path: Path, name: str) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", name, "--url", "http://localhost:7681"], apps_file)
    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file) == [{"name": name, "url": "http://localhost:7681"}]


@pytest.mark.parametrize(
    "name",
    [
        "my_app",
        "MyApp",
        "UPPER",
        "agent-abc",
        "-leading",
        "trailing-",
        "double--hyphen",
        "",
        "dot.name",
        "localhost",
    ],
)
def test_invalid_names_are_rejected_with_a_clear_error(tmp_path: Path, name: str) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", name, "--url", "http://localhost:7681"], apps_file)
    assert result.returncode != 0
    assert "invalid app name" in result.stderr or "error" in result.stderr
    # A rejected name must never reach the registry.
    assert not apps_file.exists()


def test_remove_also_rejects_invalid_names(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--remove", "--name", "my_app"], apps_file)
    assert result.returncode != 0
    assert "invalid app name" in result.stderr


def test_upsert_then_remove_round_trips(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"

    result = _run(["--name", "web", "--url", "http://localhost:5000"], apps_file)
    assert result.returncode == 0, result.stderr

    # Upsert of the same name updates the URL in place instead of appending.
    result = _run(["--name", "web", "--url", "http://localhost:5001"], apps_file)
    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file) == [{"name": "web", "url": "http://localhost:5001"}]

    result = _run(["--remove", "--name", "web"], apps_file)
    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file) == []
