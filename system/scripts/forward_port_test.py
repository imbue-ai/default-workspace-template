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


def _icon_file_args(tmp_path: Path, markup: str) -> list[str]:
    icon_file = tmp_path / "icon.svg"
    icon_file.write_text(markup)
    return ["--icon-file", str(icon_file)]


_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M2 2h12v12H2z"/></svg>'
_OTHER_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/></svg>'


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
    result = _run(["--name", name, "--url", "http://localhost:7681", "--no-icon"], apps_file)
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

    result = _run(["--name", "web", "--url", "http://localhost:5000", "--no-icon"], apps_file)
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


def test_register_from_an_icon_file_stores_the_contents_not_the_path(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    icon_file = tmp_path / "icon.svg"
    # A file on disk realistically has surrounding whitespace; the stored
    # markup is the stripped element.
    icon_file.write_text(f"\n{_ICON}\n")
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", "--icon-file", str(icon_file)],
        apps_file,
    )
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert rows[0]["icon"] == _ICON
    assert str(icon_file) not in apps_file.read_text()


def test_register_without_an_icon_omits_the_key(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", "web", "--url", "http://localhost:8000", "--no-icon"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert "icon" not in rows[0]


def test_reregistering_without_an_icon_keeps_the_one_already_set(tmp_path: Path) -> None:
    """Services re-register on every restart, usually without repeating their
    icon; that must not silently drop the icon back to the generic glyph."""
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, _ICON)], apps_file
    )
    assert result.returncode == 0, result.stderr

    result = _run(["--name", "web", "--url", "http://localhost:8001"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert rows[0]["url"] == "http://localhost:8001"
    assert rows[0]["icon"] == _ICON


def test_reregistering_with_an_icon_replaces_the_previous_one(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, _ICON)], apps_file
    )
    assert result.returncode == 0, result.stderr

    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, _OTHER_ICON)], apps_file
    )
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert rows[0]["icon"] == _OTHER_ICON


def test_register_without_internal_omits_the_key(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", "web", "--url", "http://localhost:8000", "--no-icon"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert "internal" not in rows[0]


def test_register_internal_marks_the_entry(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "owner-exec", "--url", "http://localhost:8793", "--internal"], apps_file
    )
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert rows[0]["internal"] is True


def test_reregistering_without_internal_clears_a_previously_internal_entry(tmp_path: Path) -> None:
    """Unlike the icon, ``internal`` has no tri-state to preserve: a service's
    own registration call always passes the flag or always omits it, so every
    call is authoritative rather than sticky."""
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", "--internal"], apps_file
    )
    assert result.returncode == 0, result.stderr

    result = _run(["--name", "web", "--url", "http://localhost:8001"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert "internal" not in rows[0]


def test_register_without_program_omits_the_key(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--name", "web", "--url", "http://localhost:8000", "--no-icon"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert "program" not in rows[0]


def test_register_with_program_stores_the_supervisord_program_name(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", "--no-icon", "--program", "web"], apps_file
    )
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert rows[0]["program"] == "web"


def test_reregistering_without_program_clears_a_previously_stored_one(tmp_path: Path) -> None:
    """Like ``internal`` (and unlike the icon), every registration call is
    authoritative about ``program``: a block that stops passing it must not
    leave a stale stop/start capability behind."""
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", "--no-icon", "--program", "web"], apps_file
    )
    assert result.returncode == 0, result.stderr

    result = _run(["--name", "web", "--url", "http://localhost:8001"], apps_file)
    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    assert "program" not in rows[0]


def test_an_empty_program_is_rejected(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", "--program", "  "], apps_file
    )
    assert result.returncode != 0
    assert "--program must not be empty" in result.stderr
    assert not apps_file.exists()


def test_program_cannot_be_combined_with_remove(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(["--remove", "--name", "web", "--program", "web"], apps_file)
    assert result.returncode != 0
    assert "cannot be combined with --remove" in result.stderr


def test_an_oversized_icon_is_rejected(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    forward_port = _load_module("_forward_port_icon_cap", _SCRIPT)
    padding = "0 " * forward_port.MAX_ICON_LENGTH
    oversized = f'<svg xmlns="http://www.w3.org/2000/svg"><path d="{padding}"/></svg>'
    assert len(oversized) > forward_port.MAX_ICON_LENGTH
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, oversized)], apps_file
    )
    assert result.returncode != 0
    assert "invalid icon" in result.stderr
    # A rejected icon must never reach the registry, not even as a bare row.
    assert not apps_file.exists()


@pytest.mark.parametrize(
    "payload",
    [
        "<div>not an svg</div>",
        "just some text",
        "<svg><unclosed></svg>",
        # Two elements is not "a single <svg> element".
        "<svg/><svg/>",
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
        # Anything that could execute or reach off the page.
        "<svg><script>alert(1)</script></svg>",
        '<svg onload="alert(1)"/>',
        '<svg><a href="javascript:alert(1)"><path d="M0 0"/></a></svg>',
        '<svg><image href="https://example.com/tracker.png"/></svg>',
        "<svg><style>* { display: none }</style></svg>",
    ],
)
def test_a_payload_that_is_not_a_safe_single_svg_is_rejected(tmp_path: Path, payload: str) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, payload)], apps_file
    )
    assert result.returncode != 0
    assert "invalid icon" in result.stderr
    assert not apps_file.exists()


def test_a_non_svg_icon_file_is_refused(tmp_path: Path) -> None:
    png_file = tmp_path / "icon.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = _run(["--name", "web", "--url", "http://localhost:8000", "--icon-file", str(png_file)], tmp_path / "apps.toml")
    assert result.returncode != 0
    assert "must be an .svg file" in result.stderr

def test_a_missing_icon_file_fails_loudly(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        [
            "--name",
            "web",
            "--url",
            "http://localhost:8000",
            "--icon-file",
            str(tmp_path / "nope.svg"),
        ],
        apps_file,
    )
    assert result.returncode != 0
    assert "does not exist" in result.stderr
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
