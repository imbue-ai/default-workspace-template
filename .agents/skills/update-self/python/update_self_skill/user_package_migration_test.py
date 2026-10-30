"""Tests for converting an old-layout user package to a standalone project.

The fixture is a workspace as an update leaves it: upstream's root members name only the
built-in library, while the user's app (``widget``) still leans on the root workspace -- no
lock of its own, its shared library through the root's ``{ workspace = true }`` source, and
a program running it from the root venv. Locks are real (``uv lock``, offline: the fixture
has only path dependencies); syncing and the uv cache are stood in for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from update_self_skill import user_package_migration as migration
from update_self_skill import user_packages

_OLD_PROGRAM = (
    "python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "
    '"python3 system/scripts/forward_port.py --manifest system/apps/widget/app.toml'
    ' --url http://localhost:8091 && exec .venv/bin/widget-app"'
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _root_pyproject(members: Sequence[str]) -> str:
    listed = ", ".join(f'"{member}"' for member in members)
    return f"""[project]
name = "workspace"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "applib",
    # the user's app, from before user packages were standalone
    "widget",
]

[tool.uv.workspace]
members = [{listed}]

[tool.uv.sources]
applib = {{ workspace = true }}
widget = {{ workspace = true }}
"""


class _Uv:
    """Runs ``uv lock`` and git for real (offline); answers ``uv cache dir``; records the rest."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], cwd: Path) -> migration.Completed:
        argv = list(command)
        self.calls.append(argv)
        if argv == ["uv", "cache", "dir"]:
            return migration.Completed(0, f"{self.cache_dir}\n", "")
        if argv[:2] == ["uv", "lock"] or argv[0] == "git":
            result = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                env={**os.environ, "UV_OFFLINE": "1"},
            )
            return migration.Completed(result.returncode, result.stdout, result.stderr)
        return migration.Completed(0, "", "")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    _write(
        root / "system/libs/applib/pyproject.toml",
        '[project]\nname = "applib"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
    )
    _write(
        root / "system/apps/widget/pyproject.toml",
        '[project]\nname = "widget"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        'dependencies = ["applib"]\n\n[project.scripts]\nwidget-app = "widget.runner:main"\n',
    )
    _write(
        root / user_packages.SUPERVISORD_DROPIN_DIR / "widget.conf",
        f"[program:widget]\ncommand={_OLD_PROGRAM}\ndirectory=/home/user/workspace\n",
    )
    _write(
        root / ".agents/skills/report/SKILL.md",
        "Run `uv run --no-sync widget-app --report` for today's numbers.\n",
    )
    # The lock the app ran on: the old layout, where the globs made it a member.
    _write(root / "pyproject.toml", _root_pyproject(["system/libs/*", "system/apps/*"]))
    locked = subprocess.run(
        ["uv", "lock"],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_OFFLINE": "1"},
    )
    assert locked.returncode == 0, locked.stderr
    shutil.copy(root / "uv.lock", tmp_path / "pre-merge.lock")
    # The update's merge: upstream's explicit members.
    _write(root / "pyproject.toml", _root_pyproject(["system/libs/applib"]))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_migrate_converts_an_old_layout_app_to_a_standalone_project(
    workspace: Path, tmp_path: Path
) -> None:
    uv = _Uv(tmp_path / "cache" / "uv")
    assert user_packages.standalone_problems(workspace, "system/apps/widget")

    code = migration.main(
        [
            "--repo-root",
            str(workspace),
            "migrate",
            "system/apps/widget",
            "--shared-lock",
            str(tmp_path / "pre-merge.lock"),
        ],
        run=uv,
    )

    assert code == 0
    app = workspace / "system/apps/widget"
    pyproject = tomllib.loads((app / "pyproject.toml").read_text())
    assert pyproject["tool"]["uv"]["sources"] == {
        "applib": {"path": "../../libs/applib", "editable": True}
    }
    root = tomllib.loads((workspace / "pyproject.toml").read_text())
    assert root["project"]["dependencies"] == ["applib"]
    assert root["tool"]["uv"]["sources"] == {"applib": {"workspace": True}}
    # Its own lock, locked for real from the seed, and current.
    lock = tomllib.loads((app / "uv.lock").read_text())
    assert {package["name"] for package in lock["package"]} >= {"widget", "applib"}
    check = subprocess.run(
        ["uv", "lock", "--check", "--project", "system/apps/widget"],
        cwd=workspace,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_OFFLINE": "1"},
    )
    assert check.returncode == 0, check.stderr
    # The program and the one-off now run it from its own project.
    assert user_packages.standalone_problems(workspace, "system/apps/widget") == []
    assert (workspace / ".agents/skills/report/SKILL.md").read_text() == (
        "Run `uv run --no-sync --project system/apps/widget widget-app --report` for today's numbers.\n"
    )
    assert (app / ".venv").is_symlink()
    assert ["uv", "sync", "--locked", "--project", "system/apps/widget"] in uv.calls


def test_a_dry_run_says_what_it_would_change_and_changes_nothing(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {
        path: path.read_text()
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }

    code = migration.main(
        ["--repo-root", str(workspace), "migrate", "system/apps/widget", "--dry-run"],
        run=_Uv(tmp_path / "cache" / "uv"),
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "`applib` becomes an editable path source (../../libs/applib)" in out
    assert "`widget` leaves the root pyproject's dependencies and sources" in out
    assert "rewrites system/supervisord.conf.d/widget.conf" in out
    assert "rewrites .agents/skills/report/SKILL.md" in out
    after = {
        path: path.read_text()
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert after == before


@pytest.mark.parametrize(
    "command",
    [
        "python3 system/services/oom_priority/bin/oom_tag_service.py user .venv/bin/widget-app",
        "python3 system/services/oom_priority/bin/oom_tag_service.py user uv run widget-app --port 9",
        _OLD_PROGRAM,
        'bash -c "uv sync --all-packages --frozen && exec nice -n 5 .venv/bin/widget-app"',
        'bash -c "exec python3 system/scripts/with_secrets.py data/.secrets/w.env -- .venv/bin/widget-app"',
    ],
)
def test_every_old_program_form_becomes_one_the_standalone_check_accepts(
    command: str,
) -> None:
    rewritten = migration._program_rewrite(
        command, "system/apps/widget", ["widget-app"]
    )

    assert rewritten is not None
    assert (
        user_packages.program_problems(rewritten, "system/apps/widget", ["widget-app"])
        == []
    )
    # Whatever wrapped it before still wraps it.
    for kept in (
        "oom_tag_service.py user",
        "forward_port.py",
        "with_secrets.py",
        "nice -n 5",
        "--port 9",
    ):
        if kept in command:
            assert kept in rewritten


@pytest.mark.parametrize(
    "run",
    [
        "uv run widget-app --port 9",
        "uv run --package widget --frozen widget-app --port 9",
        "uv run --with flask --python 3.12 widget-app --port 9",
    ],
)
def test_a_uv_run_becomes_an_exec_of_the_script_with_none_of_uvs_options(
    run: str,
) -> None:
    # The standalone check accepts a line whose exec still carries a stray option value
    # (`exec widget .../widget-app`), so the exec step itself is what has to be right.
    rewritten = migration._program_rewrite(
        f"python3 system/services/oom_priority/bin/oom_tag_service.py user {run}",
        "system/apps/widget",
        ["widget-app"],
    )

    assert rewritten is not None
    assert user_packages.command_steps(rewritten)[-1] == [
        "exec",
        "system/apps/widget/.venv/bin/widget-app",
        "--port",
        "9",
    ]


def test_a_program_that_runs_another_packages_script_is_left_alone() -> None:
    assert (
        migration._program_rewrite(
            "python3 system/services/oom_priority/bin/oom_tag_service.py chat .venv/bin/chat-app",
            "system/apps/widget",
            ["widget-app"],
        )
        is None
    )


def test_relock_holds_a_directly_declared_root_pinned_package_to_the_root_pin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(
        root / "pyproject.toml",
        "[tool.uv.workspace]\nmembers = []\n\n[tool.uv.sources]\n"
        'imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "abc123", subdirectory = "libs/mngr" }\n',
    )
    app = root / "system/apps/widget"
    _write(
        app / "pyproject.toml",
        '[project]\nname = "widget"\ndependencies = ["imbue-mngr", "flask"]\n',
    )
    # Locked with no source of its own, it took imbue-mngr from the index.
    _write(
        app / "uv.lock",
        '[[package]]\nname = "imbue-mngr"\nversion = "0.2.0"\nsource = { registry = "https://pypi.org/simple" }\n',
    )
    locks: list[str] = []

    def run(command: Sequence[str], cwd: Path) -> migration.Completed:
        locks.append(" ".join(command))
        return migration.Completed(0, "", "")

    pinned = migration.align_pins(root, "system/apps/widget", run)

    assert pinned == ["imbue-mngr"]
    sources = tomllib.loads((app / "pyproject.toml").read_text())["tool"]["uv"][
        "sources"
    ]
    assert sources == {
        "imbue-mngr": {
            "git": "https://github.com/imbue-ai/mngr",
            "rev": "abc123",
            "subdirectory": "libs/mngr",
        }
    }
    assert locks == ["uv lock --project system/apps/widget"]


def test_a_package_that_is_still_a_root_member_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write(
        root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["system/apps/*"]\n'
    )
    _write(root / "system/apps/widget/pyproject.toml", '[project]\nname = "widget"\n')

    with pytest.raises(migration.MigrationError, match="root workspace member"):
        migration.plan_migration(
            root, "system/apps/widget", None, migration.run_subprocess
        )


@pytest.mark.parametrize(
    ("test_source", "dependency"),
    [
        ("def test_runs() -> None:\n    pass\n", "pytest"),
        (
            "from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc\n",
            "imbue-common[testing]",
        ),
    ],
)
def test_a_package_with_tests_gets_what_they_run_with_and_its_own_pytest_root(
    workspace: Path, test_source: str, dependency: str
) -> None:
    _write(workspace / "system/apps/widget/src/widget/runner_test.py", test_source)

    plan = migration.plan_migration(
        workspace, "system/apps/widget", None, migration.run_subprocess
    )

    pyproject = tomllib.loads(plan.package_text)
    assert pyproject["dependency-groups"]["dev"] == [dependency]
    assert pyproject["tool"]["pytest"]["ini_options"] == {"testpaths": ["."]}
