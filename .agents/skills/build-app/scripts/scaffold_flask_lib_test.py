"""What the scaffolder writes -- and, just as importantly, what it leaves alone.

A scaffold's whole footprint has to be files only it owns, so that two agents can
build two creations in one workspace at the same time. That is a property of the
files the script touches, not of the lib it generates, so these run the real script
over a real (temporary) workspace and assert on the tree it leaves behind.

The port pre-flight is checked the same way: every program declares its port in
its own drop-in now, so a pre-flight that read only the main config would hand a
new app a port another program already holds.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "scaffold_flask_lib.py"

# The shipped shape: the main config declares no programs at all, only the
# daemon's own sections and the [include] that pulls in the drop-ins.
_MAIN_CONF = """\
[supervisord]
logfile=/var/log/supervisor/supervisord.log

[include]
files = supervisord.conf.d/*.conf
"""

# A workspace that predates the split, or one whose mind moved a program back:
# the scaffolder still reads the main config, so a program declared there has to
# be seen by both the port pre-flight and the name guard.
_MAIN_CONF_WITH_INLINE_PROGRAM = _MAIN_CONF + """
[program:system_interface]
command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
"""

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
        f'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:{port} --name {name} && uv run --all-packages {name}"'
        if port is not None
        else f"command=uv run --all-packages {name}"
    )
    return f"[program:{name}]\n{command}\ndirectory=/home/user/workspace\n"


def _make_workspace(
    root: Path, dropins: dict[str, int | None], main_conf: str = _MAIN_CONF
) -> Path:
    (root / "system/supervisord.conf.d").mkdir(parents=True)
    (root / "system/supervisord.conf").write_text(main_conf)
    (root / "pyproject.toml").write_text(_ROOT_PYPROJECT)
    for name, port in dropins.items():
        (root / f"system/supervisord.conf.d/{name}.conf").write_text(_dropin(name, port))
    return root


def _scaffold(root: Path, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--name",
            name,
            "--description",
            "a test app",
            "--repo-root",
            str(root),
            "--skip-uv-sync",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_scaffold_authors_only_its_own_files(tmp_path: Path) -> None:
    """The program lands in its own drop-in; no shared file is edited.

    `uv.lock` is the one shared file a real scaffold rewrites, and only because
    `uv sync` regenerates it -- skipped here, as it is derived rather than authored.
    """
    root = _make_workspace(tmp_path, {"browser": 8081, "app-watcher": None})
    before_conf = (root / "system/supervisord.conf").read_text()
    before_pyproject = (root / "pyproject.toml").read_text()

    result = _scaffold(root, "news")
    assert result.returncode == 0, result.stderr

    program = (root / "system/supervisord.conf.d/news.conf").read_text()
    assert "[program:news]" in program
    # A scaffolded member is not a root dependency, so a root-closure-scoped
    # `uv sync` prunes it; --all-packages is what reinstates it on restart.
    assert "uv run --all-packages news" in program
    assert (root / "system/apps/news/src/news/runner.py").exists()

    assert (root / "system/supervisord.conf").read_text() == before_conf
    assert (root / "pyproject.toml").read_text() == before_pyproject


def test_a_program_declared_in_the_main_config_is_still_seen(tmp_path: Path) -> None:
    """The main config is scanned too, not just the drop-ins.

    Every program ships in a drop-in now, but the scaffolder must not assume it:
    a workspace predating the split declares its programs inline, and a mind is
    free to move one back. Both its port and its name have to be respected.
    """
    root = _make_workspace(
        tmp_path, {"browser": 8081}, main_conf=_MAIN_CONF_WITH_INLINE_PROGRAM
    )

    taken_port = _scaffold(root, "news", "--port", "8000")
    assert taken_port.returncode != 0
    assert "already in use" in taken_port.stderr

    taken_name = _scaffold(root, "system-interface")
    assert taken_name.returncode != 0

    # 8000 and 8081 are both held, so the auto pick steps over them.
    ok = _scaffold(root, "news")
    assert ok.returncode == 0, ok.stderr
    assert "http://localhost:8080" in (root / "system/supervisord.conf.d/news.conf").read_text()


def test_auto_picked_port_avoids_a_port_held_by_a_dropin(tmp_path: Path) -> None:
    """8080 and 8081 are taken by drop-ins alone, so the next app gets 8082."""
    root = _make_workspace(tmp_path, {"browser": 8081, "dashboard": 8080})

    result = _scaffold(root, "news")
    assert result.returncode == 0, result.stderr

    assert "http://localhost:8082" in (root / "system/supervisord.conf.d/news.conf").read_text()


def test_a_dropin_the_include_glob_would_not_read_is_refused(tmp_path: Path) -> None:
    """Which directory holds the drop-ins is the config's to declare, not the scaffolder's.

    A workspace whose ``[include]`` points somewhere else would otherwise get a
    drop-in supervisord never reads: the app simply never starts, and nothing
    fails. Refusing is the only outcome the agent can act on.
    """
    root = _make_workspace(
        tmp_path,
        {},
        main_conf=_MAIN_CONF.replace("files = supervisord.conf.d/*.conf", "files = programs.d/*.conf"),
    )

    result = _scaffold(root, "news")

    assert result.returncode != 0
    assert "no [include] glob" in result.stderr
    assert not (root / "system/supervisord.conf.d/news.conf").exists()
    assert not (root / "system/apps").exists()


def test_a_port_held_by_a_non_default_include_directory_is_still_seen(tmp_path: Path) -> None:
    """The port pre-flight follows the declared globs, so a renamed directory is still scanned."""
    root = _make_workspace(
        tmp_path,
        {},
        main_conf=_MAIN_CONF.replace(
            "files = supervisord.conf.d/*.conf",
            "files = %(here)s/programs.d/*.conf supervisord.conf.d/*.conf",
        ),
    )
    (root / "system/programs.d").mkdir()
    (root / "system/programs.d/dashboard.conf").write_text(_dropin("dashboard", 8080))

    result = _scaffold(root, "news")
    assert result.returncode == 0, result.stderr

    assert "http://localhost:8081" in (root / "system/supervisord.conf.d/news.conf").read_text()


def test_requested_port_held_by_a_dropin_is_refused(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path, {"browser": 8081})

    result = _scaffold(root, "news", "--port", "8081")

    assert result.returncode != 0
    assert "8081 is already in use" in result.stderr
    assert not (root / "system/supervisord.conf.d/news.conf").exists()


def test_name_already_declared_by_a_dropin_is_refused(tmp_path: Path) -> None:
    """Two programs of one name would collide; ``browser`` is not in RESERVED_NAMES.

    The refusal has to come before anything is written: a half-scaffolded lib
    left in the tree is foreign dirt the next hardening pass cannot clean.
    """
    root = _make_workspace(tmp_path, {"browser": 8081})

    result = _scaffold(root, "browser")

    assert result.returncode != 0
    assert "supervisord.conf.d/browser.conf" in result.stderr
    assert not (root / "system/apps").exists()


def test_name_held_by_an_event_listener_is_refused(tmp_path: Path) -> None:
    """supervisord holds programs and event listeners in one process-group namespace.

    A duplicate there does not just shadow the other declaration -- it breaks the
    config for every program at the next reread.
    """
    root = _make_workspace(tmp_path, {})
    (root / "system/supervisord.conf.d/oom-tag-backstop.conf").write_text(
        "[eventlistener:oom-tag-backstop]\ncommand=python3 backstop.py\n"
    )

    result = _scaffold(root, "oom-tag-backstop")

    assert result.returncode != 0
    assert "[eventlistener:oom-tag-backstop]" in result.stderr
    assert not (root / "system/apps").exists()
