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
from app_manifest.primitives import describe_app_name_problem

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

def test_a_bad_icon_file_does_not_brick_an_existing_apps_restart(tmp_path: Path) -> None:
    """A corrupted icon file fails a NEW registration, but an already-registered
    app must still restart: warn, register without it, keep the stored icon."""
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, _ICON)], apps_file
    )
    assert result.returncode == 0, result.stderr

    bad_file = tmp_path / "icon.svg"
    bad_file.write_text("<div>not an svg</div>")
    result = _run(["--name", "web", "--url", "http://localhost:8001", "--icon-file", str(bad_file)], apps_file)
    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr
    rows = _read_apps(apps_file)
    assert rows[0]["url"] == "http://localhost:8001"
    assert rows[0]["icon"] == _ICON

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


def test_app_manifest_name_rule_is_identical_to_the_registration_rule() -> None:
    """Drift guard: the app_manifest library validates names on read with its
    own copy of this script's rule (the script is stdlib-only and cannot import
    the library). The two must accept and reject exactly the same names."""
    forward_port = _load_module("_forward_port_manifest_drift_check", _SCRIPT)
    names = (
        "web",
        "my-app2",
        "a",
        "app2",
        "openvscode-server-4",
        "x9-y",
        "system_interface",
        "under_score",
        "double--hyphen",
        "host-foo",
        "agent-foo",
        "localhost",
        "auth",
        "-lead",
        "trail-",
        "UPPER",
        "dot.name",
        "",
        "a" * 32,
        "a" * 33,
    )
    for name in names:
        is_script_accepted = forward_port.validate_service_name(name) is None
        is_library_accepted = describe_app_name_problem(name) is None
        assert is_script_accepted == is_library_accepted, name


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


# --- manifests -----------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str, icon: str | None = _ICON) -> Path:
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    if icon is not None:
        (app_dir / "icon.svg").write_text(icon)
    manifest = app_dir / "app.toml"
    manifest.write_text(body)
    return manifest


_FULL_MANIFEST = """
name = "files"
display_name = "File Viewer"
icon = "icon.svg"
instances = true
instances_url = "http://127.0.0.1:8301"
critical = false
priority = "files"

[default_shortcut]
action = "new"
mode = "focus"

[[actions]]
id = "new"
label = "New File Viewer"
params = [{name = "path", label = "Path", required = false}]
"""


def test_manifest_registration_copies_every_field_onto_the_row(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)

    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300"], apps_file)

    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "files"
    assert row["url"] == "http://localhost:8300"
    assert _LABEL_RE.match(row["label"])
    assert row["icon"] == _ICON
    assert row["display_name"] == "File Viewer"
    assert row["instances"] is True
    assert row["instances_url"] == "http://127.0.0.1:8301"
    assert row["critical"] is False
    assert row["priority"] == "files"
    assert row["program"] == "files"
    assert "internal" not in row
    assert row["default_shortcut"] == {"action": "new", "mode": "focus"}
    # ``params`` is documentation for the manifest's readers; the row carries id and label only.
    assert row["actions"] == [{"id": "new", "label": "New File Viewer"}]


def test_manifest_registration_is_authoritative_on_every_call(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)
    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300"], apps_file)
    assert result.returncode == 0, result.stderr
    label_after_create = _read_apps(apps_file)[0]["label"]

    # A changed manifest: fewer fields, a different display name, a different program.
    manifest.write_text('name = "files"\ndisplay_name = "Files"\nicon = "icon.svg"\nprogram = "files-sidecar"\ncritical = true\n')
    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8301"], apps_file)

    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == label_after_create
    assert row["url"] == "http://localhost:8301"
    assert row["display_name"] == "Files"
    assert row["program"] == "files-sidecar"
    assert row["critical"] is True
    for stale_key in ("instances", "instances_url", "default_shortcut", "actions"):
        assert stale_key not in row, stale_key
    assert "priority" not in row


def test_manifest_registration_with_a_matching_name_flag_is_accepted(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)

    result = _run(["--manifest", str(manifest), "--name", "files", "--url", "http://localhost:8300"], apps_file)

    assert result.returncode == 0, result.stderr
    assert _read_apps(apps_file)[0]["name"] == "files"


def test_manifest_whose_name_differs_from_the_name_flag_is_refused(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)

    result = _run(["--manifest", str(manifest), "--name", "other", "--url", "http://localhost:8300"], apps_file)

    assert result.returncode != 0
    assert "does not match the manifest's name" in result.stderr
    assert not apps_file.exists()


def test_an_internal_manifest_needs_no_icon_and_marks_the_row_internal(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(
        tmp_path,
        'name = "system_interface"\ndisplay_name = "Workspace"\ninternal = true\ncritical = true\npriority = "system_interface"\n',
        icon=None,
    )

    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8000"], apps_file)

    assert result.returncode == 0, result.stderr
    row = _read_apps(apps_file)[0]
    assert row["internal"] is True
    assert row["program"] == "system_interface"
    assert "icon" not in row


def test_a_manifest_with_an_invalid_name_is_refused(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, 'name = "Bad Name"\ndisplay_name = "X"\nicon = "icon.svg"\n')

    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300"], apps_file)

    assert result.returncode != 0
    assert "invalid app name" in result.stderr
    assert not apps_file.exists()


def test_a_manifest_with_a_wrongly_typed_field_is_refused(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, 'name = "web"\ndisplay_name = "Web"\nicon = "icon.svg"\ninstances = "yes"\n')

    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300"], apps_file)

    assert result.returncode != 0
    assert "instances must be a boolean" in result.stderr


def test_a_manifest_registration_cannot_combine_the_per_flag_forms(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)

    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300", "--program", "files"], apps_file)

    assert result.returncode != 0
    assert "--manifest cannot be combined" in result.stderr


def test_a_manifest_less_registration_writes_exactly_the_keys_it_always_has(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        ["--name", "web", "--url", "http://localhost:8000", *_icon_file_args(tmp_path, _ICON), "--program", "web"],
        apps_file,
    )

    assert result.returncode == 0, result.stderr
    assert set(_read_apps(apps_file)[0]) == {"name", "url", "label", "icon", "program"}


def test_a_manifest_missing_its_icon_file_does_not_brick_an_existing_apps_restart(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    manifest = _write_manifest(tmp_path, _FULL_MANIFEST)
    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8300"], apps_file)
    assert result.returncode == 0, result.stderr

    (manifest.parent / "icon.svg").unlink()
    result = _run(["--manifest", str(manifest), "--url", "http://localhost:8301"], apps_file)

    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr
    row = _read_apps(apps_file)[0]
    assert row["url"] == "http://localhost:8301"
    assert row["icon"] == _ICON


# --- the stdlib writer ------------------------------------------------------------


def test_the_writer_round_trips_an_icon_with_quotes_newlines_and_the_real_files_icon(tmp_path: Path) -> None:
    forward_port = _load_module("_forward_port_writer_check", _SCRIPT)
    real_icon = (Path(__file__).parents[2] / "system" / "apps" / "files" / "icon.svg").read_text().strip()
    awkward_icon = '<svg xmlns="http://www.w3.org/2000/svg"\n  viewBox="0 0 24 24">\n\t<path d="M2 2h20"/>\\\n</svg>'
    apps = [
        {"name": "files", "url": "http://localhost:8300", "label": "files-abcd1234", "icon": real_icon, "instances": True},
        {
            "name": "web",
            "url": "http://localhost:8000",
            "label": "web-abcd1234",
            "icon": awkward_icon,
            "internal": True,
            "default_shortcut": {"action": "new", "mode": "focus"},
            "actions": [{"id": "new", "label": 'Say "hi"'}, {"id": "other", "label": "Other"}],
        },
    ]

    rendered = forward_port.dump_registry(apps)

    assert tomllib.loads(rendered)["apps"] == apps


def test_the_writer_escapes_control_characters(tmp_path: Path) -> None:
    forward_port = _load_module("_forward_port_writer_escape_check", _SCRIPT)
    apps = [{"name": "web", "url": "http://localhost:8000", "label": "bell\x07 and \x7f"}]

    rendered = forward_port.dump_registry(apps)

    assert "\\u0007" in rendered and "\\u007F" in rendered
    assert tomllib.loads(rendered)["apps"] == apps


def test_the_writer_refuses_a_value_type_the_registry_never_holds() -> None:
    forward_port = _load_module("_forward_port_writer_type_check", _SCRIPT)
    with pytest.raises(TypeError, match="cannot hold") as excinfo:
        forward_port.dump_registry([{"name": "web", "port": 8000}])
    assert isinstance(excinfo.value, forward_port.RegistryError)


def test_a_legacy_registry_written_by_tomlkit_is_read_and_rewritten_intact(tmp_path: Path) -> None:
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text(
        '[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n\n'
        '[[apps]]\nname = "files"\nurl = "http://localhost:8300"\nlabel = "files-abcd1234"\n'
        'icon = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n<path d="M2 2h20v20H2z"/></svg>"""\n'
        'program = "files"\n'
    )

    result = _run(["--name", "web", "--url", "http://localhost:8000", "--no-icon"], apps_file)

    assert result.returncode == 0, result.stderr
    rows = _read_apps(apps_file)
    assert [row["name"] for row in rows] == ["terminal", "files", "web"]
    assert rows[1]["icon"] == '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n<path d="M2 2h20v20H2z"/></svg>'
    assert rows[1]["label"] == "files-abcd1234"


def test_the_script_imports_under_an_isolated_stdlib_only_interpreter() -> None:
    # Every supervisord program line runs this script under a plain python3
    # before its own command, so registration must never depend on the venv.
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import importlib.util, sys; spec = importlib.util.spec_from_file_location('fp', {str(_SCRIPT)!r}); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.DEFAULT_APPS_FILE)"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "data/.state/apps.toml"
