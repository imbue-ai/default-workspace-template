"""Tests for the app-port registry script.

The script upserts / removes entries in an apps.toml registry and validates
that the app name can serve as the leading label of a service origin hostname
(``<name>.host-<hex>.localhost`` locally; share hostnames follow the same
prefix rule on a longer base). We drive it end to end as a subprocess -- the
way supervisord and services invoke it -- with ``MINDS_APPS_FILE`` pointed at
a sandboxed registry.
"""

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent / "forward_port.py"

# ``<name>-<8 lowercase base36 chars>``.
_LABEL_RE = re.compile(r"^[a-z0-9_-]+-[a-z0-9]{8}$")


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


@pytest.mark.parametrize(
    "name",
    [
        "terminal",
        "browser",
        "my-app",
        "app2",
        "a",
        "openvscode-server-4",
        "system_interface",
    ],
)
def test_valid_names_register_and_are_persisted(tmp_path: Path, name: str) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", name, "--url", "http://localhost:7681"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert rows[0]["name"] == name
    assert rows[0]["url"] == "http://localhost:7681"
    # A registered service gets an unguessable ``<name>-<rand>`` origin label.
    assert rows[0]["label"].startswith(f"{name}-")
    assert _LABEL_RE.match(rows[0]["label"])


@pytest.mark.parametrize(
    "name",
    [
        "MyApp",
        "UPPER",
        "host-abc",
        "agent-abc",
        "-leading",
        "trailing-",
        "double--hyphen",
        "",
        "dot.name",
        "localhost",
    ],
)
def test_invalid_names_are_rejected_with_a_clear_error(
    tmp_path: Path, name: str
) -> None:
    apps_file = tmp_path / "apps.toml"
    # ``--name=<value>`` keeps argparse from eating a leading-hyphen name as
    # an option token, so every case exercises the validator itself.
    result = _run([f"--name={name}", "--url", "http://localhost:7681"], apps_file)
    assert result.returncode != 0
    assert "invalid app name" in result.stderr
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

    label_after_create = _read_apps(apps_file)[0]["label"]

    # Upsert of the same name updates the URL in place instead of appending,
    # and keeps the original label (a service's origin must be stable).
    result = _run(["--name", "web", "--url", "http://localhost:5001"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert rows[0]["name"] == "web"
    assert rows[0]["url"] == "http://localhost:5001"
    assert rows[0]["label"] == label_after_create

    result = _run(["--remove", "--name", "web"], apps_file)
    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file) == []


def test_name_over_the_length_cap_is_rejected(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    too_long = "a" * 33
    result = _run(["--name", too_long, "--url", "http://localhost:7681"], apps_file)
    assert result.returncode != 0
    assert "invalid app name" in result.stderr
    assert not apps_file.exists()


def test_auth_name_is_reserved(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", "auth", "--url", "http://localhost:7681"], apps_file)
    assert result.returncode != 0
    assert "invalid app name" in result.stderr
    assert not apps_file.exists()


def _load_module(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scaffold_name_rule_stays_a_subset_of_the_registration_rule() -> None:
    """Drift guard: the build-app scaffold's name validation must stay a
    subset of this script's, or the scaffold could mint an app whose
    ``forward_port.py`` registration then fails at runtime. (The scaffold is
    deliberately stricter -- letter-start, no underscores, its own reserved
    list -- but every name it accepts must register cleanly.)
    """
    repo_root = Path(__file__).parents[2]
    forward_port = _load_module("_forward_port_drift_check", _SCRIPT)
    scaffold = _load_module(
        "_scaffold_drift_check",
        repo_root
        / ".agents"
        / "skills"
        / "build-app"
        / "scripts"
        / "scaffold_flask_lib.py",
    )
    names = (
        "web",
        "my-app2",
        "a",
        "app2",
        "openvscode-server-4",
        "x9-y",
        "double--hyphen",
        "host-foo",
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
            and not any(
                name.startswith(prefix) for prefix in scaffold.RESERVED_NAME_PREFIXES
            )
            and name not in scaffold.RESERVED_NAMES
            and scaffold._kebab_to_snake(name) not in scaffold.RESERVED_NAMES
        )
        if is_scaffold_accepted:
            assert forward_port.validate_service_name(name) is None, name
