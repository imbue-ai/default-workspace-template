"""What the scaffolder writes -- and, just as importantly, what it leaves alone.

A scaffold's whole footprint has to be files only it owns, so that two agents can
build two creations in one workspace at the same time. That is a property of the
files the script touches, not of the lib it generates, so these run the real script
over a real (temporary) workspace and assert on the tree it leaves behind.

The scaffold registers the app through the installer (``app_install``), which
allocates its port and generates its supervisord program, so what these check
about a port or a program is read back from the record and the generated file a
scaffold leaves. Every program declares its port in its own drop-in, so an
allocation that read only the main config would hand a new app a port another
program already holds. The port choice -- the auto pick and the refusal of a
requested port -- runs in-process with a bind probe the test supplies, because
the real probe asks this machine, and a live workspace already has apps
listening across the auto-pick range. The supplied probe reports nothing bound,
except in the test of the probe itself. The tests that run the real script
assert only what holds whatever this machine is listening on.
"""

from __future__ import annotations

import ast
import base64
import configparser
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from app_install.ports import LOWEST_AUTOMATIC_PORT
from app_manifest.manifest import load_manifest
from app_manifest.primitives import MAX_APP_DESCRIPTION_LENGTH, MAX_DISPLAY_NAME_LENGTH
from app_manifest.records import AppOrigin, load_record, record_path
from app_manifest.testing import commit_everything, init_git_repository
from imbue.app_sdk.api_schema import NO_SCHEMA_KEY
from imbue.app_sdk.shell_client import SHELL_URL_ENVIRONMENT_VARIABLE
from loguru import logger
from workspace_bare_scripts import venv_links

from build_app_skill import scaffold_flask_lib
from build_app_skill.testing import ShellStandIn

_SCRIPT = Path(__file__).resolve().with_name("scaffold_flask_lib.py")

_REPO_ROOT = Path(__file__).resolve().parents[5]

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'

_DESCRIPTION = "a test app"

# A whole second pytest session in a subprocess, which a cold or loaded box (gVisor especially)
# can run past the suite's 10s per-test budget.
_APP_TEST_RUN_TIMEOUT_SECONDS = 60

# The shipped shape: the main config declares no programs at all, only the
# daemon's own sections and the [include] that pulls in the drop-ins.
_MAIN_CONF = """\
[supervisord]
logfile=/var/log/supervisor/supervisord.log

[include]
files = supervisord.conf.d/*.conf
"""

# A workspace that predates the split, or one whose agent moved a program back:
# the main config is still read, so a program declared there has to be seen by
# both the port allocation and the name guard. The program is
# ``dashboard`` rather than a real built-in because a name in RESERVED_NAMES is
# refused before any config is read, which would make the name check below pass
# without the main config being scanned at all. Its port is inside the auto-pick
# range (which starts at 8080) so that the auto pick has to step over it -- a
# port below the range would be invisible to the auto pick either way.
_MAIN_CONF_WITH_INLINE_PROGRAM = (
    _MAIN_CONF
    + """
[program:dashboard]
command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8080 --name dashboard && dashboard"
"""
)

_ROOT_PYPROJECT = """\
[project]
name = "workspace"
version = "0.1.0"
dependencies = ["bootstrap"]

[tool.uv.workspace]
members = ["system/apps/*"]
"""


def _dropin(name: str, port: int | None) -> str:
    command = (
        f'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:{port} --name {name} && {name}"'
        if port is not None
        else f"command={name}"
    )
    return f"[program:{name}]\n{command}\ndirectory=/home/user/workspace\n"


def _make_workspace(
    root: Path, dropins: dict[str, int | None], main_conf: str = _MAIN_CONF
) -> Path:
    (root / "system/supervisord.conf.d").mkdir(parents=True)
    (root / "system/supervisord.conf").write_text(main_conf)
    (root / "pyproject.toml").write_text(_ROOT_PYPROJECT)
    for name, port in dropins.items():
        (root / f"system/supervisord.conf.d/{name}.conf").write_text(
            _dropin(name, port)
        )
    return root


def _nothing_bound(port: int) -> bool:
    return False


def _scaffold(root: Path, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
    icon = root.parent / "icon.svg"
    icon.write_text(_ICON)
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--name",
            name,
            "--description",
            _DESCRIPTION,
            "--icon-file",
            str(icon),
            "--repo-root",
            str(root),
            "--skip-uv-sync",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def _write_lib(repo_root: Path, name: str) -> Path:
    """The app directory the scaffold writes for ``name``, not yet registered."""
    return scaffold_flask_lib._write_lib(
        repo_root,
        name,
        _DESCRIPTION,
        scaffold_flask_lib._display_name(_DESCRIPTION, None),
        [],
        _ICON,
        None,
        [],
    )


def _write_inbox_status_lib(repo_root: Path) -> Path:
    return scaffold_flask_lib._write_lib(
        repo_root,
        "inbox-status",
        "inbox status dashboard",
        "Inbox status",
        [],
        _ICON,
        None,
        [],
    )


def _register(
    root: Path,
    name: str,
    requested_port: int | None = None,
    is_port_bound: Callable[[int], bool] = _nothing_bound,
) -> int:
    """Write and register the app ``name`` in-process, with a supplied bind probe; the port it was given."""
    _write_lib(root, name)
    port, _ = scaffold_flask_lib._register_app(
        root, name.replace("-", "_"), requested_port, is_port_bound
    )
    return port


def _program(root: Path, name: str) -> configparser.SectionProxy:
    """The generated supervisord program of the app ``name``."""
    parser = configparser.RawConfigParser()
    parser.read_string((root / f"system/supervisord.conf.d/{name}.conf").read_text())
    return parser[f"program:{name}"]


def _files(root: Path) -> dict[str, str]:
    """Every file under ``root`` by relative path, with its text."""
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_nothing_left_of(root: Path, name: str) -> None:
    """A refused scaffold of ``name`` left no app directory, no record and no program behind."""
    assert not (root / "system/apps" / name.replace("-", "_")).exists()
    assert not record_path(root, name).exists()
    assert not (root / f"system/supervisord.conf.d/{name}.conf").exists()


def test_a_scaffolded_app_gives_up_instead_of_crash_looping(tmp_path: Path) -> None:
    """A broken app must reach FATAL, not restart at full speed forever.

    An app that keeps dying is restarted by supervisord, and each start
    re-registers it, so an unbounded retry policy turns one broken app into a
    permanent restart-and-re-register loop nobody is told about (measured on a
    real workspace: 46,939 restarts in a day). Asserted as the property --
    a finite retry count, and a startsecs long enough that a fast death counts
    as a failed start -- rather than as the exact numbers.
    """
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})

    assert _scaffold(root, "news").returncode == 0

    program = _program(root, "news")
    assert int(program["startretries"]) <= 20
    assert float(program["startsecs"]) >= 10


def test_scaffold_authors_only_its_own_files(tmp_path: Path) -> None:
    """The app's directory, its record and its program are all a scaffold writes; no shared file is edited.

    The app is a standalone project, so a real scaffold writes its own `uv.lock` and
    never the root one (the acceptance test below locks and syncs for real).
    """
    root = _make_workspace(
        tmp_path / "workspace", {"browser": 8081, "host-backup": None}
    )
    before = _files(root)

    result = _scaffold(root, "news")
    assert result.returncode == 0, result.stderr

    after = _files(root)
    assert {path: text for path, text in after.items() if path in before} == before
    written_outside_the_app = {
        path
        for path in after.keys() - before.keys()
        if not path.startswith("system/apps/news/")
    }
    # The installer's lock is machine state under data/, which no repository tracks.
    assert written_outside_the_app == {
        "system/config/apps/news.toml",
        "system/supervisord.conf.d/news.conf",
        "data/.state/app_install/lock",
    }
    assert "system/apps/news/src/news/runner.py" in after

    record = load_record(root, "news")
    assert (record.package, record.origin) == ("news", AppOrigin.LOCAL)
    # The app registers its own manifest and runs its own entry point, so
    # nothing about it lives in a file another creation also writes.
    command = _program(root, "news")["command"]
    assert "--manifest system/apps/news/app.toml" in command
    assert command.endswith('&& exec system/apps/news/.venv/bin/news"')
    # The scaffold says which files are the app's record and its generated program.
    assert "system/config/apps/news.toml" in result.stdout
    assert "system/supervisord.conf.d/news.conf" in result.stdout
    assert "app-install regenerate news" in result.stdout


def test_a_program_declared_in_the_main_config_is_still_seen(tmp_path: Path) -> None:
    """The main config is scanned too, not just the drop-ins.

    Every program ships in a drop-in, but the scaffolder must not assume it:
    a workspace predating the split declares its programs inline, and an agent is
    free to move one back. Both its port and its name have to be respected.
    """
    root = _make_workspace(
        tmp_path / "workspace",
        {"browser": 8081},
        main_conf=_MAIN_CONF_WITH_INLINE_PROGRAM,
    )

    taken_port = _scaffold(root, "news", "--port", "8080")
    assert taken_port.returncode != 0
    assert "8080 is already in use" in taken_port.stderr
    _assert_nothing_left_of(root, "news")

    taken_name = _scaffold(root, "dashboard")
    assert taken_name.returncode != 0
    assert (
        "the program 'dashboard' is already declared in system/supervisord.conf\n"
        in taken_name.stderr
    )
    _assert_nothing_left_of(root, "dashboard")

    # 8080 is held by the main config and 8081 by the drop-in, so the auto pick
    # lands on 8082 -- it would answer 8080 if the main config went unread.
    assert _register(root, "news") == 8082


def test_auto_picked_port_avoids_a_port_held_by_a_dropin(tmp_path: Path) -> None:
    """8080 and 8081 are taken by drop-ins alone, so the next app gets 8082, and the one after it steps over 8083."""
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081, "dashboard": 8080})

    assert _register(root, "news") == 8082
    # 8083 is the Screen's CDP proxy, which has no program line, and news holds 8082.
    assert _register(root, "notes") == 8084


def test_the_screen_video_server_port_is_never_picked(tmp_path: Path) -> None:
    """8091 is the Screen's neko port, which no config scan or bind probe sees.

    With 8080-8090 taken, the auto pick must skip 8091 and an explicit --port 8091
    must be refused, or the new app takes the port and the Screen cannot start.
    """
    root = _make_workspace(
        tmp_path / "workspace", {f"app{port}": port for port in range(8080, 8091)}
    )

    assert _register(root, "news") == 8092
    refused = _scaffold(root, "notes", "--port", "8091")
    assert refused.returncode != 0
    assert "8091 is already in use" in refused.stderr
    _assert_nothing_left_of(root, "notes")


def test_a_port_something_is_listening_on_is_skipped_and_refused(
    tmp_path: Path,
) -> None:
    """No config holds 8080, so only the bind probe can keep the scaffold off it."""
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})

    def only_8080_bound(port: int) -> bool:
        return port == 8080

    assert _register(root, "news", is_port_bound=only_8080_bound) == 8082
    with pytest.raises(SystemExit, match="8080 is already in use"):
        _register(root, "notes", 8080, only_8080_bound)
    _assert_nothing_left_of(root, "notes")


def test_the_port_is_in_the_record_and_the_program_and_in_no_file_of_the_app(
    tmp_path: Path,
) -> None:
    """The port a scaffold is given reaches the app through its program's environment alone.

    This runs the real script, so its bind probe asks this machine and the exact
    port depends on what is listening here. The assertion is what holds on any
    machine: the record, the program's registration and the program's environment
    carry one port, it is not one the config holds, and the app's directory, which
    may later be installed in another workspace on another port, does not carry it.
    """
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081, "dashboard": 8080})

    result = _scaffold(root, "news")
    assert result.returncode == 0, result.stderr

    port = load_record(root, "news").port
    assert port >= LOWEST_AUTOMATIC_PORT
    assert port not in {8080, 8081}
    program = _program(root, "news")
    assert f"--url http://localhost:{port} " in program["command"]
    assert program["environment"] == f'NEWS_PORT="{port}"'
    runner = (root / "system/apps/news/src/news/runner.py").read_text()
    assert runner == scaffold_flask_lib._lib_runner(
        "news",
        "news",
        _DESCRIPTION,
        scaffold_flask_lib._display_name(_DESCRIPTION, None),
    )
    assert [
        path
        for path, text in _files(root / "system/apps/news").items()
        if str(port) in text
    ] == []


def test_a_requested_port_is_the_one_the_app_is_registered_on(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})
    port = _free_port()

    result = _scaffold(root, "news", "--port", str(port))
    assert result.returncode == 0, result.stderr

    assert load_record(root, "news").port == port
    assert _program(root, "news")["environment"] == f'NEWS_PORT="{port}"'


def test_a_directory_matching_the_include_glob_does_not_break_the_scan(
    tmp_path: Path,
) -> None:
    """A glob matches whatever is on disk, and a directory can be named like a drop-in.

    Every consumer of the expansion reads what it is handed, so an unfiltered match turns the
    port allocation into an IsADirectoryError traceback -- a scaffold that dies on an unrelated
    directory someone happened to create. supervisord cannot read it either, so skipping it
    loses nothing.
    """
    root = _make_workspace(tmp_path / "workspace", {"dashboard": 8080})
    (root / "system/supervisord.conf.d/archive.conf").mkdir()

    # The real drop-in beside it was still scanned: 8080 is taken, so the new app gets 8081.
    assert _register(root, "notes") == 8081

    result = _scaffold(root, "news")

    assert result.returncode == 0, result.stderr


def test_requested_port_held_by_a_dropin_is_refused(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})

    with pytest.raises(SystemExit, match="8081 is already in use"):
        _register(root, "notes", 8081)
    _assert_nothing_left_of(root, "notes")

    result = _scaffold(root, "news", "--port", "8081")

    assert result.returncode != 0
    assert "8081 is already in use" in result.stderr
    _assert_nothing_left_of(root, "news")


def test_name_already_declared_by_a_dropin_is_refused(tmp_path: Path) -> None:
    """Two programs of one name would collide; ``browser`` is not in RESERVED_NAMES.

    The refusal leaves nothing behind: a half-scaffolded lib left in the tree is
    foreign dirt the next hardening pass cannot clean, and the program that holds
    the name is untouched.
    """
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})
    before = _files(root)

    result = _scaffold(root, "browser")

    assert result.returncode != 0
    assert (
        "the program 'browser' is already declared in system/supervisord.conf.d/browser.conf"
        in result.stderr
    )
    assert not (root / "system/apps/browser").exists()
    assert not record_path(root, "browser").exists()
    after = _files(root)
    assert {path: text for path, text in after.items() if path in before} == before
    assert after.keys() - before.keys() == {"data/.state/app_install/lock"}


def test_name_held_by_an_event_listener_is_refused(tmp_path: Path) -> None:
    """supervisord holds programs and event listeners in one process-group namespace.

    A duplicate there does not just shadow the other declaration -- it breaks the
    config for every program at the next reread.
    """
    root = _make_workspace(tmp_path / "workspace", {})
    (root / "system/supervisord.conf.d/oom-tag-backstop.conf").write_text(
        "[eventlistener:oom-tag-backstop]\ncommand=python3 backstop.py\n"
    )

    result = _scaffold(root, "oom-tag-backstop")

    assert result.returncode != 0
    assert (
        "the program 'oom-tag-backstop' is already declared in "
        "system/supervisord.conf.d/oom-tag-backstop.conf" in result.stderr
    )
    assert not (root / "system/apps/oom_tag_backstop").exists()
    assert not record_path(root, "oom-tag-backstop").exists()


def test_a_refusal_names_every_problem(tmp_path: Path) -> None:
    """One run tells the agent everything that has to change, rather than one collision per attempt."""
    root = _make_workspace(tmp_path / "workspace", {"news": 8081})
    other = root / "system/apps/headlines"
    other.mkdir(parents=True)
    (other / "app.toml").write_text('name = "news"\n')

    result = _scaffold(root, "news")

    assert result.returncode != 0
    assert (
        "the name 'news' is already the app at system/apps/headlines" in result.stderr
    )
    assert (
        "the program 'news' is already declared in system/supervisord.conf.d/news.conf"
        in result.stderr
    )
    assert not (root / "system/apps/news").exists()
    assert not record_path(root, "news").exists()


def test_write_lib_writes_a_manifest_the_library_accepts(tmp_path: Path) -> None:
    lib_dir = _write_inbox_status_lib(tmp_path)

    manifest = load_manifest(lib_dir / "app.toml")

    assert lib_dir == tmp_path / "system" / "apps" / "inbox_status"
    assert manifest.name == "inbox-status"
    assert manifest.display_name == "Inbox status"
    assert manifest.icon == "icon.svg"
    assert manifest.priority == "user"
    assert manifest.program == "inbox-status"
    assert manifest.default_shortcuts == ()
    assert manifest.description == "inbox status dashboard"
    assert manifest.api_schema_path == "/api/schema"
    assert manifest.secrets == ()
    assert (lib_dir / "src" / "inbox_status" / "runner_test.py").exists()


def test_the_manifest_and_the_pyproject_keep_a_description_with_quotes_and_characters_outside_the_basic_plane(
    tmp_path: Path,
) -> None:
    description = 'notes, "quoted", with a clef \U0001d11e and an accent \u00e9'

    lib_dir = scaffold_flask_lib._write_lib(
        tmp_path, "notes", description, "Notes", [], _ICON, None, []
    )

    assert load_manifest(lib_dir / "app.toml").description == description
    pyproject = tomllib.loads((lib_dir / "pyproject.toml").read_text())
    assert pyproject["project"]["description"] == description


def test_the_scaffold_does_not_register_a_manifest_the_library_would_skip(
    tmp_path: Path,
) -> None:
    lib_dir = _write_inbox_status_lib(tmp_path)
    (lib_dir / "icon.svg").unlink()

    with pytest.raises(SystemExit) as refused:
        scaffold_flask_lib._register_app(
            tmp_path, "inbox_status", None, _nothing_bound
        )

    assert str(refused.value).startswith(
        "error: the workspace refused to register the app, so system/apps/inbox_status "
        "was removed again:"
    )
    assert "icon" in str(refused.value)
    assert not lib_dir.exists()
    assert not (tmp_path / "system/config/apps/inbox-status.toml").exists()


def test_the_scaffold_does_not_register_a_manifest_key_the_library_does_not_know(
    tmp_path: Path,
) -> None:
    """Registration reads the manifest as its author: a reader would warn about the key and go on."""
    lib_dir = _write_inbox_status_lib(tmp_path)
    manifest_path = lib_dir / "app.toml"
    manifest_path.write_text('no_such_key = "x"\n' + manifest_path.read_text())

    with pytest.raises(SystemExit) as refused:
        scaffold_flask_lib._register_app(
            tmp_path, "inbox_status", None, _nothing_bound
        )

    assert "no_such_key" in str(refused.value)
    assert not lib_dir.exists()


def test_the_scaffolded_preview_table_names_the_variables_the_runner_reads(
    tmp_path: Path,
) -> None:
    """A preview boots the app by setting the table's variables, so each one has to be one the runner reads."""
    lib_dir = _write_inbox_status_lib(tmp_path)

    preview = load_manifest(lib_dir / "app.toml").preview
    runner = (lib_dir / "src/inbox_status/runner.py").read_text()

    assert set(preview.env) == {
        "INBOX_STATUS_PORT",
        "INBOX_STATUS_HOST",
        "INBOX_STATUS_DATA_DIR",
    }
    for variable in preview.env:
        assert f'os.environ.get("{variable}"' in runner


def _test_function_names(source: str) -> frozenset[str]:
    return frozenset(
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def test_a_scaffolded_apps_ratchets_define_the_tests_every_project_defines() -> None:
    """test_meta_ratchets holds every project's ratchets file to one set of tests, so an
    app the scaffold writes must carry exactly that set or its first commit fails it."""
    reference = _REPO_ROOT / "system/libs/app_manifest/test_app_manifest_ratchets.py"

    assert _test_function_names(
        scaffold_flask_lib._lib_ratchets()
    ) == _test_function_names(reference.read_text())


def test_a_scaffolded_app_passes_its_own_type_check(tmp_path: Path) -> None:
    lib_dir = _write_inbox_status_lib(tmp_path)

    result = subprocess.run(
        [str(Path(sys.executable).parent / "ty"), "check", "--python", sys.prefix],
        cwd=lib_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.timeout(_APP_TEST_RUN_TIMEOUT_SECONDS)
def test_a_scaffolded_app_passes_its_own_tests(tmp_path: Path) -> None:
    """The tests the scaffold writes pass on the code it writes beside them, before the agent adds anything.

    The ratchets list the app's files through git and blame the lines they flag, so the app is
    committed first. Its type-check ratchet is left out: it runs ``uv run ty``, which outside a
    workspace would install the app's dependencies, and
    ``test_a_scaffolded_app_passes_its_own_type_check`` runs ty on a scaffolded app directly.
    """
    lib_dir = _write_inbox_status_lib(tmp_path)
    init_git_repository(tmp_path)
    commit_everything(tmp_path, "scaffold inbox-status")
    # Options the outer run was given are not the app's: a `-k` naming this test would deselect all of its tests.
    inherited = {
        name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"
    }

    # import-path: no-venv -- outside a workspace the scaffolded app has no venv of its own, so its tests run from this one
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--deselect",
            "test_inbox_status_ratchets.py::test_no_type_errors",
        ],
        cwd=lib_dir,
        env={**inherited, "PYTHONPATH": str(lib_dir / "src")},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_display_name_falls_back_to_the_description() -> None:
    assert (
        scaffold_flask_lib._display_name("inbox status dashboard", None)
        == "inbox status dashboard"
    )
    assert (
        scaffold_flask_lib._display_name("inbox status dashboard", " Inbox ") == "Inbox"
    )


@pytest.mark.parametrize("candidate", ["", "   ", "x" * 65, 'say "hi"'])
def test_display_name_refuses_what_the_manifest_would_not_take(candidate: str) -> None:
    with pytest.raises(SystemExit):
        scaffold_flask_lib._display_name("description", candidate)


@pytest.mark.parametrize("candidate", ["", "   ", "two\nlines", "x" * 201])
def test_a_description_the_manifest_would_refuse_is_refused_before_anything_is_written(
    candidate: str,
) -> None:
    with pytest.raises(SystemExit):
        scaffold_flask_lib._check_description(candidate)


def test_the_description_limit_matches_the_library() -> None:
    assert scaffold_flask_lib.MAX_APP_DESCRIPTION_LENGTH == MAX_APP_DESCRIPTION_LENGTH


def test_the_display_name_limit_matches_the_library() -> None:
    # The scaffold keeps the library out of its imports, so it carries its own
    # copy of the limit.
    assert scaffold_flask_lib.MAX_DISPLAY_NAME_LENGTH == MAX_DISPLAY_NAME_LENGTH


def test_the_runner_page_connects_to_the_shell_and_installs_the_element_menu() -> None:
    source = scaffold_flask_lib._lib_runner(
        "inbox-status",
        "inbox_status",
        "inbox status dashboard",
        "inbox status dashboard",
    )
    assert 'from "/_static/app_contract.js"' in source
    assert 'from "/_static/context_menu.js"' in source
    assert "connection.location(location.pathname + location.search" in source
    assert (
        "installElementContextMenu({ connection, handshake: () => handshake })"
        in source
    )
    assert "minds-location" not in source


@pytest.mark.parametrize(
    ("secrets_arguments", "named_in_the_refusal"),
    [
        # The file's name is a slug every other reader of data/.secrets/ enforces.
        (
            ["--secrets-file", "My Secret", "--secrets-variable", "NEWS_API_KEY"],
            "--secrets-file",
        ),
        # The manifest's [[secrets]] table needs a variable, and the program is generated from it.
        (["--secrets-file", "news-api"], "--secrets-variable"),
        (["--secrets-variable", "NEWS_API_KEY"], "--secrets-file"),
        (
            ["--secrets-file", "news-api", "--secrets-variable", "news-api-key"],
            "--secrets-variable 'news-api-key'",
        ),
        (
            ["--secrets-file", "news-api", "--secrets-variable", "1KEY"],
            "--secrets-variable '1KEY'",
        ),
        (
            [
                "--secrets-file",
                "news-api",
                "--secrets-variable",
                "NEWS_API_KEY",
                "--secrets-variable",
                "NEWS_API_KEY",
            ],
            "twice",
        ),
    ],
)
def test_a_secret_declaration_the_manifest_would_refuse_is_refused_before_anything_is_written(
    tmp_path: Path, secrets_arguments: list[str], named_in_the_refusal: str
) -> None:
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})
    before = _files(root)

    refused = _scaffold(root, "news", *secrets_arguments)

    assert refused.returncode != 0
    assert named_in_the_refusal in refused.stderr
    assert _files(root) == before


def test_a_declared_secrets_file_is_in_the_manifest_and_wraps_the_entry_point_in_with_secrets(
    tmp_path: Path,
) -> None:
    """The app must read its key through the wrapper and nothing else: the manifest declares the
    file and its variables, and the program generated from it carries the wrapper in front of the
    entry point, and only when a file was named."""
    root = _make_workspace(tmp_path / "workspace", {"browser": 8081})

    plain = _scaffold(root, "widget-app")
    assert plain.returncode == 0, plain.stderr
    assert _program(root, "widget-app")["command"].endswith(
        '&& exec system/apps/widget_app/.venv/bin/widget-app"'
    )

    wrapped = _scaffold(
        root,
        "news",
        "--secrets-file",
        "news-api",
        "--secrets-variable",
        "NEWS_API_KEY",
        "--secrets-variable",
        "NEWS_API_SECRET",
    )
    assert wrapped.returncode == 0, wrapped.stderr
    (secret,) = load_manifest(root / "system/apps/news/app.toml").secrets
    assert (secret.file, secret.variables) == (
        "news-api",
        ("NEWS_API_KEY", "NEWS_API_SECRET"),
    )
    assert _program(root, "news")["command"].endswith(
        "&& exec python3 system/scripts/with_secrets.py data/.secrets/news-api.env"
        ' -- system/apps/news/.venv/bin/news"'
    )


def _import_scaffolded_runner(tmp_path: Path) -> ModuleType:
    """Write a scaffolded runner.py into ``tmp_path`` and import it."""
    source = scaffold_flask_lib._lib_runner(
        "inbox-status",
        "inbox_status",
        "inbox status dashboard",
        "inbox status dashboard",
    )
    runner = tmp_path / "runner.py"
    runner.write_text(source)
    spec = importlib.util.spec_from_file_location("scaffolded_runner", runner)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runner_serves_the_shell_modules_and_nothing_else(
    tmp_path: Path, shell_stand_in: ShellStandIn
) -> None:
    """The scaffolded app answers the two shell-built modules from its own origin, and 404s any other name."""
    shell_stand_in.body_by_path["/_static/app_contract.js"] = (
        b"export function connectToShell() {}\n"
    )
    client = _import_scaffolded_runner(tmp_path).app.test_client()
    served = client.get("/_static/app_contract.js")
    assert served.status_code == 200
    assert served.mimetype == "text/javascript"
    assert b"connectToShell" in served.data
    # The shell has no such module to give, so neither does the app.
    assert client.get("/_static/context_menu.js").status_code == 404
    assert client.get("/_static/runner.py").status_code == 404
    assert client.get("/_static/..%2Frunner.py").status_code == 404
    # Nothing but the modules is ever asked of the shell, so the route cannot be made to fetch another path.
    assert shell_stand_in.requested_paths == [
        "/_static/app_contract.js",
        "/_static/context_menu.js",
    ]
    page = client.get("/")
    assert page.status_code == 200
    assert b'from "/_static/context_menu.js"' in page.data
    document = client.get("/api/schema").get_json()
    assert document["info"] == {
        "title": "inbox status dashboard",
        "version": "1.0",
        "description": "inbox status dashboard",
    }
    assert set(document["paths"]) == {
        "/",
        "/_csp-report",
        "/_static/{basename}",
        "/health",
    }
    # The scaffold's route table: the health probe answers its model through the table, and the page and the
    # shell modules say why they carry no schema.
    health = client.get("/health")
    assert (health.status_code, health.get_json()) == (200, {"status": "ok"})
    assert document["paths"]["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/HealthResponse"}
    assert "HealthResponse" in document["components"]["schemas"]
    assert NO_SCHEMA_KEY in document["paths"]["/"]["get"]
    assert NO_SCHEMA_KEY in document["paths"]["/_static/{basename}"]["get"]
    assert NO_SCHEMA_KEY in document["paths"]["/_csp-report"]["post"]
    # The page's policy admits its one inline script by the hash of the body as served, and nothing inline beyond it.
    body = (
        page.get_data(as_text=True)
        .split('<script type="module">', 1)[1]
        .split("</script>", 1)[0]
    )
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    policy = page.headers["Content-Security-Policy"]
    assert f"script-src 'self' 'report-sample' 'sha256-{digest}';" in policy
    assert policy.startswith("default-src 'self'; ")
    assert "; img-src 'self' data: blob:; " in policy
    assert policy.endswith("; report-uri /_csp-report")
    assert "Content-Security-Policy" not in client.get("/health").headers


def test_the_runner_serves_its_page_with_no_shell_to_fetch_the_modules_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app run on its own, outside a workspace, still serves: the modules are a 404 and the page loads."""
    monkeypatch.setenv(
        SHELL_URL_ENVIRONMENT_VARIABLE, f"http://127.0.0.1:{_free_port()}"
    )
    client = _import_scaffolded_runner(tmp_path).app.test_client()

    assert client.get("/_static/app_contract.js").status_code == 404
    assert client.get("/_static/context_menu.js").status_code == 404
    assert client.get("/").status_code == 200
    assert client.get("/health").get_json() == {"status": "ok"}


def test_the_runner_logs_what_its_policy_blocked(tmp_path: Path) -> None:
    """A browser's report of a blocked resource is logged as an error naming the resource, the page and the directive.

    That line is how an agent learns a page pulled in something the policy does not admit: the
    page itself shows nothing server-side, and the supervisor log is what check-app-errors reads.
    A blocked inline script is reported only as "inline", so the line adds where it is and how it
    starts (the sample the policy's 'report-sample' asks the browser for) and names the hash remedy.
    A block caused by a browser extension is the policy working and stays out of the log, and a
    body that is not a report is answered without a trace.
    """
    client = _import_scaffolded_runner(tmp_path).app.test_client()
    logged: list[str] = []
    sink_id = logger.add(
        lambda message: logged.append(
            f"{message.record['level'].name} {message.record['message']}"
        ),
        level="DEBUG",
    )
    try:
        report = client.post(
            "/_csp-report",
            data=json.dumps(
                {
                    "csp-report": {
                        "document-uri": "http://127.0.0.1:8081/",
                        "blocked-uri": "https://cdn.jsdelivr.net/npm/chart.js",
                        "effective-directive": "script-src-elem",
                        "violated-directive": "script-src-elem",
                    }
                }
            ),
            content_type="application/csp-report",
        )
        assert report.status_code == 204
        assert logged == [
            "ERROR Content-Security-Policy blocked 'https://cdn.jsdelivr.net/npm/chart.js' on "
            "'http://127.0.0.1:8081/' (script-src-elem); "
            "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
        ]
        logged.clear()

        inline = {
            "csp-report": {
                "document-uri": "http://127.0.0.1:8081/stats",
                "blocked-uri": "inline",
                "effective-directive": "script-src-elem",
                "source-file": "http://127.0.0.1:8081/stats",
                "line-number": 42,
                "column-number": 3,
                "script-sample": "const chart = new Chart(document.getElemen",
            }
        }
        assert client.post("/_csp-report", json=inline).status_code == 204
        assert logged == [
            "ERROR Content-Security-Policy blocked 'inline' on 'http://127.0.0.1:8081/stats' (script-src-elem) "
            "at http://127.0.0.1:8081/stats:42 starting 'const chart = new Chart(document.getElemen'; "
            "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        ]
        logged.clear()

        from_extension = {
            "csp-report": {
                "document-uri": "http://127.0.0.1:8081/",
                "blocked-uri": "inline",
                "source-file": "chrome-extension://abcdef/content.js",
                "effective-directive": "script-src-elem",
            }
        }
        assert client.post("/_csp-report", json=from_extension).status_code == 204
        assert (
            client.post("/_csp-report", json=["not", "a", "report"]).status_code == 204
        )
        assert client.post("/_csp-report", data=b"\x00garbage").status_code == 204
        assert logged == []
    finally:
        logger.remove(sink_id)


def test_the_runner_does_not_use_reloader() -> None:
    source = scaffold_flask_lib._lib_runner(
        "inbox-status",
        "inbox_status",
        "inbox status dashboard",
        "inbox status dashboard",
    )
    assert "use_reloader=False" in source


def test_main_parser_supports_start_flag() -> None:
    # Check that --start is accepted by the parser
    # We can invoke scaffold_flask_lib with --help or inspect the parser in main
    # Or test by creating an ArgumentParser with the same arguments
    import inspect

    main_src = inspect.getsource(scaffold_flask_lib.main)
    assert '"--start"' in main_src


def _evaluated_runner(tmp_path: Path) -> dict[str, object]:
    source = scaffold_flask_lib._lib_runner(
        "inbox-status",
        "inbox_status",
        "inbox status dashboard",
        "inbox status dashboard",
    )
    namespace: dict[str, object] = {"__name__": "inbox_status.runner"}
    exec(compile(source, str(tmp_path / "runner.py"), "exec"), namespace)
    return namespace


def test_the_runner_module_evaluates_its_environment_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner reads its address and data dir from the environment at import; a
    runner whose imports do not cover that read crashes before serving anything."""
    monkeypatch.setenv("INBOX_STATUS_PORT", "8123")
    monkeypatch.setenv("INBOX_STATUS_HOST", "127.0.0.2")
    monkeypatch.setenv("INBOX_STATUS_DATA_DIR", str(tmp_path / "copy"))

    namespace = _evaluated_runner(tmp_path)

    assert namespace["PORT"] == 8123
    assert namespace["HOST"] == "127.0.0.2"
    assert namespace["DATA_DIR"] == tmp_path / "copy"


def test_the_runner_holds_no_port_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing in its environment the runner asks the operating system for a free port on
    loopback: the port a workspace gives the app is the workspace's, and is in no file of the app."""
    for variable in ("INBOX_STATUS_PORT", "INBOX_STATUS_HOST", "INBOX_STATUS_DATA_DIR"):
        monkeypatch.delenv(variable, raising=False)

    namespace = _evaluated_runner(tmp_path)

    assert namespace["PORT"] == 0
    assert namespace["HOST"] == "127.0.0.1"
    assert namespace["DATA_DIR"] == Path("data/.apps/inbox-status")
    source = scaffold_flask_lib._lib_runner(
        "inbox-status",
        "inbox_status",
        "inbox status dashboard",
        "inbox status dashboard",
    )
    assert re.findall(r"\d{4,5}", source) == []


def _workspace_for_a_real_lock(root: Path) -> Path:
    """A workspace uv can resolve a scaffolded app against, without the whole tree.

    The root pyproject and lock as they are, every root member's pyproject (uv reads them
    to discover the workspace), the full source of the libraries a scaffolded app
    installs, and the venv-link helper its generated program runs.
    """
    root.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copy(_REPO_ROOT / "uv.lock", root / "uv.lock")
    members = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["tool"]["uv"][
        "workspace"
    ]["members"]
    for member in members:
        for directory in _REPO_ROOT.glob(member):
            relative = directory.relative_to(_REPO_ROOT)
            (root / relative).mkdir(parents=True, exist_ok=True)
            shutil.copy(
                directory / "pyproject.toml", root / relative / "pyproject.toml"
            )
    for library in (
        "system/libs/app_schema",
        "system/libs/app_manifest",
        "system/libs/app_registration",
        "system/libs/workspace_layout",
    ):
        shutil.rmtree(root / library)
        shutil.copytree(
            _REPO_ROOT / library,
            root / library,
            ignore=shutil.ignore_patterns(".venv", "__pycache__"),
        )
    helper = Path("system/scripts/workspace_bare_scripts/venv_links.py")
    (root / helper.parent).mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / helper, root / helper)
    (root / "system/supervisord.conf.d").mkdir(parents=True)
    (root / "system/supervisord.conf").write_text(_MAIN_CONF)
    return root


def _remove_venv_target(app: Path) -> None:
    link = app / ".venv"
    if link.is_symlink():
        venv_links.remove_target(Path(os.path.realpath(link)))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.acceptance
@pytest.mark.timeout(600)
def test_a_scaffolded_app_is_a_standalone_project_that_boots_from_its_own_venv(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    root = _workspace_for_a_real_lock(tmp_path / "workspace")
    port = _free_port()
    before_lock = (root / "uv.lock").read_text()

    result = _scaffold_for_real(root, "widget-check", port)
    app = root / "system/apps/widget_check"
    # The app's venv lives beside the real uv cache (the scaffold resolves from the warm
    # one), so the test removes it rather than leave it to the next sweep.
    request.addfinalizer(lambda: _remove_venv_target(app))

    assert result.returncode == 0, result.stdout + result.stderr
    # Its own lock and a linked venv; the root lock is untouched.
    assert (app / "uv.lock").is_file()
    assert (app / ".venv").is_symlink()
    assert (root / "uv.lock").read_text() == before_lock
    lock = tomllib.loads((app / "uv.lock").read_text())
    by_name = {package["name"]: package for package in lock["package"]}
    root_lock = tomllib.loads(before_lock)
    root_versions = {
        package["name"]: package.get("version") for package in root_lock["package"]
    }
    # Seeded from the root lock: a package both use stays on the built-ins' version, and
    # imbue-common comes from the root's mngr pin through app-schema's sources.
    assert by_name["flask"]["version"] == root_versions["flask"]
    assert by_name["imbue-common"]["source"] == next(
        package["source"]
        for package in root_lock["package"]
        if package["name"] == "imbue-common"
    )
    # The harden pass's browser tests run from this venv, which the repo root's does not reach.
    assert by_name["playwright"]["version"] == root_versions["playwright"]
    assert "pytest-timeout" in by_name
    pyproject = tomllib.loads((app / "pyproject.toml").read_text())
    marks = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert {mark.split(":")[0] for mark in marks} == {"browser", "frontend"}
    # The generated program, run as supervisord would (less the registration with the shell,
    # which needs a live workspace), boots the app from its own venv on the port the program's
    # environment gives it.
    assert load_record(root, "widget-check").port == port
    program = _program(root, "widget-check")
    assert program["environment"] == f'WIDGET_CHECK_PORT="{port}"'
    words = shlex.split(program["command"])
    steps = [step.strip() for step in words[words.index("-c") + 1].split("&&")]
    script = " && ".join(step for step in steps if "forward_port" not in step)
    assert script.endswith("exec system/apps/widget_check/.venv/bin/widget-check")
    booted = subprocess.Popen(
        ["bash", "-c", script],
        cwd=root,
        env={**os.environ, "WIDGET_CHECK_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        body = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and booted.poll() is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as response:
                    body = json.loads(response.read())
                    break
            except OSError:
                time.sleep(0.2)
        assert body == {"status": "ok"}, booted.stdout.read() if booted.stdout else ""
    finally:
        booted.terminate()
        booted.wait(timeout=10)
    # Its tests run from the same venv, which the sync gave the dev group (the ratchets
    # list files through git, so the workspace is committed first).
    init_git_repository(root)
    commit_everything(root, "the scaffolded workspace")
    tests = subprocess.run(
        ["uv", "run", "--no-sync", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=app,
        capture_output=True,
        text=True,
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


def _scaffold_for_real(
    root: Path, name: str, port: int
) -> subprocess.CompletedProcess[str]:
    icon = root.parent / "icon.svg"
    icon.write_text(_ICON)
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--name",
            name,
            "--description",
            _DESCRIPTION,
            "--icon-file",
            str(icon),
            "--repo-root",
            str(root),
            "--port",
            str(port),
        ],
        capture_output=True,
        text=True,
    )
