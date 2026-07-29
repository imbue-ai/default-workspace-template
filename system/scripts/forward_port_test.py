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


@pytest.mark.parametrize("name", ["terminal", "browser", "my-app", "app2", "a", "openvscode-server-4", "system_interface"])
def test_valid_names_register_and_are_persisted(tmp_path: Path, name: str) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", name, "--url", "http://localhost:7681"], apps_file)
    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file) == [{"name": name, "url": "http://localhost:7681"}]


@pytest.mark.parametrize(
    "name",
    [
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
    result = _run(["--remove", "--name", "double--hyphen"], apps_file)
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


def test_scaffold_name_rule_stays_a_subset_of_the_registration_rule() -> None:
    """Drift guard: the build-app scaffold's name validation must stay a
    subset of this script's, or the scaffold could mint an app whose
    ``forward_port.py`` registration then fails at runtime. (The scaffold is
    deliberately stricter -- letter-start, its own reserved list -- but every
    name it accepts must register cleanly.)
    """
    import importlib.util

    repo_root = Path(__file__).parents[2]

    def _load(module_name: str, path: Path):
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    forward_port = _load("_forward_port_drift_check", Path(__file__).parent / "forward_port.py")
    scaffold = _load(
        "_scaffold_drift_check",
        repo_root / ".agents" / "skills" / "build-app" / "scripts" / "scaffold_flask_lib.py",
    )
    names = (
        "web",
        "my-app2",
        "a",
        "app2",
        "openvscode-server-4",
        "x9-y",
        "double--hyphen",
        "agent-foo",
        "localhost",
        "terminal",
        "-lead",
        "trail-",
        "UPPER",
        "under_score",
    )
    for name in names:
        is_scaffold_accepted = (
            bool(scaffold.KEBAB_RE.match(name))
            and not name.startswith("agent-")
            and name not in scaffold.RESERVED_NAMES
            and scaffold._kebab_to_snake(name) not in scaffold.RESERVED_NAMES
        )
        if is_scaffold_accepted:
            assert forward_port.validate_service_name(name) is None, name
