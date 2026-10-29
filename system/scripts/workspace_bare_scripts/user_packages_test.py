"""Tests for telling user packages from built-in ones and judging whether each is standalone."""

from __future__ import annotations

from pathlib import Path

import pytest

from workspace_bare_scripts import user_packages

_OWN_LINE = (
    'python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "'
    "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure . system/apps/widget"
    " && uv sync --locked --project system/apps/widget"
    " && python3 system/scripts/forward_port.py --manifest system/apps/widget/app.toml"
    " --url http://localhost:8091"
    ' && exec system/apps/widget/.venv/bin/widget-app"'
)


def _root(tmp_path: Path, members: list[str]) -> Path:
    listed = ", ".join(f'"{member}"' for member in members)
    (tmp_path / "pyproject.toml").write_text(
        f"[project]\nname = 'root'\n\n[tool.uv.workspace]\nmembers = [{listed}]\n"
    )
    return tmp_path


def _package(
    repo_root: Path, relative: str, *, scripts: tuple[str, ...] = (), extra: str = ""
) -> Path:
    directory = repo_root / relative
    directory.mkdir(parents=True)
    script_lines = "".join(f'{name} = "pkg.runner:main"\n' for name in scripts)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "{directory.name}"\n\n[project.scripts]\n{script_lines}{extra}'
    )
    return directory


def _program(repo_root: Path, name: str, command: str) -> None:
    dropins = repo_root / user_packages.SUPERVISORD_DROPIN_DIR
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / f"{name}.conf").write_text(f"[program:{name}]\ncommand={command}\n")


def test_user_packages_are_the_unlisted_ones_under_system(tmp_path: Path) -> None:
    repo_root = _root(tmp_path, ["system/apps/chat", "system/libs/app_schema", "system/scripts"])
    for relative in (
        "system/apps/chat",
        "system/apps/widget",
        "system/libs/app_schema",
        "system/libs/helpers",
        "system/services/poller",
    ):
        _package(repo_root, relative)
    (repo_root / "system" / "apps" / "manifest_only").mkdir()

    assert user_packages.user_packages(repo_root) == [
        "system/apps/widget",
        "system/services/poller",
        "system/libs/helpers",
    ]


def test_a_member_glob_makes_every_matching_package_built_in(tmp_path: Path) -> None:
    repo_root = _root(tmp_path, ["system/apps/*", "system/libs/*"])
    _package(repo_root, "system/apps/widget")
    _package(repo_root, "system/services/poller")

    assert user_packages.user_packages(repo_root) == ["system/services/poller"]


def test_a_standalone_package_has_no_problems(tmp_path: Path) -> None:
    repo_root = _root(tmp_path, [])
    package = _package(
        repo_root,
        "system/apps/widget",
        scripts=("widget-app",),
        extra='\n[tool.uv.sources]\napp-schema = { path = "../../libs/app_schema", editable = true }\n',
    )
    (package / "uv.lock").write_text("version = 1\n")
    _program(repo_root, "widget", _OWN_LINE)

    assert user_packages.standalone_problems(repo_root, "system/apps/widget") == []


def test_an_old_layout_package_names_every_problem(tmp_path: Path) -> None:
    repo_root = _root(tmp_path, [])
    _package(
        repo_root,
        "system/apps/widget",
        scripts=("widget-app",),
        extra="\n[tool.uv.sources]\napp-schema = { workspace = true }\n",
    )
    _program(
        repo_root,
        "widget",
        "python3 system/services/oom_priority/bin/oom_tag_service.py user .venv/bin/widget-app",
    )

    assert user_packages.standalone_problems(repo_root, "system/apps/widget") == [
        "it has no uv.lock of its own",
        "its source for `app-schema` is `{ workspace = true }`",
        "the `widget` program: it runs `widget-app` from somewhere other than "
        "system/apps/widget/.venv/bin",
        "the `widget` program: it does not run `uv sync --locked --project "
        "system/apps/widget` before starting it",
    ]


@pytest.mark.parametrize(
    "command",
    [
        _OWN_LINE,
        # Wrapped in the secrets reader, as an app that declares [[secrets]] is.
        'bash -c "uv sync --locked --project system/apps/widget && exec python3'
        " system/scripts/with_secrets.py data/.secrets/widget.env --"
        ' system/apps/widget/.venv/bin/widget-app"',
        # Extra steps between the sync and the exec, and an absolute entry point.
        'bash -c "cd /home/user/workspace; uv sync --directory system/apps/widget --locked'
        " && echo starting && exec nice -n 5"
        ' /home/user/workspace/system/apps/widget/.venv/bin/widget-app --port 1"',
    ],
)
def test_a_program_line_is_judged_by_what_it_runs(command: str) -> None:
    assert user_packages.program_problems(command, "system/apps/widget", ["widget-app"]) == []


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        (
            'bash -c "uv sync --locked --project system/apps/widget && exec .venv/bin/widget-app"',
            "from somewhere other than",
        ),
        (
            'bash -c "uv sync --frozen --project system/apps/widget'
            ' && exec system/apps/widget/.venv/bin/widget-app"',
            "does not run `uv sync --locked",
        ),
        (
            'bash -c "exec system/apps/widget/.venv/bin/widget-app'
            ' && uv sync --locked --project system/apps/widget"',
            "does not run `uv sync --locked",
        ),
        ("uv run widget-app", "from somewhere other than"),
    ],
)
def test_a_program_line_that_skips_its_own_sync_or_venv_is_named(
    command: str, problem: str
) -> None:
    problems = user_packages.program_problems(command, "system/apps/widget", ["widget-app"])
    assert any(problem in found for found in problems), problems


def test_a_program_that_runs_none_of_the_packages_scripts_is_not_its_own() -> None:
    assert (
        user_packages.program_problems(
            "python3 system/services/oom_priority/bin/oom_tag_service.py chat .venv/bin/chat-app",
            "system/apps/widget",
            ["widget-app"],
        )
        == []
    )


def test_the_check_cli_fails_naming_the_migration_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = _root(tmp_path, [])
    _package(repo_root, "system/libs/helpers")

    code = user_packages.main(["--repo-root", str(repo_root), "check"])

    captured = capsys.readouterr()
    assert code == 1
    assert "system/libs/helpers: it has no uv.lock of its own" in captured.out
    assert user_packages.MIGRATION_REFERENCE in captured.err
