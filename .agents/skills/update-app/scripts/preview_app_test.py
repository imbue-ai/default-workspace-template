"""Tests for ``preview_app.py``.

Run via: ``uv run pytest .agents/skills/update-app/scripts/preview_app_test.py``

A recording runner stands in for the shared ``serve_isolated_instance.py``: it records
each invocation and files the instance state the real script would, so the resolution
of a manifest's preview table, the registry copy, the one-pass guard, and the sibling
order are all asserted on the exact commands the script produces.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "preview_app.py"
_spec = importlib.util.spec_from_file_location("preview_app", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'

_CHAT_MANIFEST = """
name = "chat"
display_name = "Chat"
icon = "icon.svg"
instances = true
critical = true
priority = "chat"

[[actions]]
id = "new"
label = "New Chat"

[preview]
command = ["chat-app", "--secondary", "--nudge-shell-url", "{shell_url}"]
env = {CHAT_PORT = "{port:main}", CHAT_HOST = "{host}", CHAT_DATA_DIR = "{copy:data}"}
copies = {data = "data/.apps/chat"}
health_path = "/api/health"
open_path = "/{key}"
open_path_takes_key = true
"""

_SHELL_MANIFEST = """
name = "system_interface"
display_name = "Workspace"
internal = true
critical = true
priority = "system_interface"

[preview]
command = ["system-interface", "--preview", "--state-dir", "{copy:state}"]
env = {SYSTEM_INTERFACE_PORT = "{port:main}", MINDS_APPS_FILE = "{registry}"}
copies = {state = "data/.state/system_interface"}
health_path = "/api/health"
"""

_NOTES_MANIFEST = """
name = "notes"
display_name = "Notes"
icon = "icon.svg"
"""


def _write_worktree(root: Path) -> Path:
    worktree = root / "worktree"
    for package, manifest in (
        ("chat", _CHAT_MANIFEST),
        ("system_interface", _SHELL_MANIFEST),
        ("notes", _NOTES_MANIFEST),
    ):
        app_dir = worktree / "system" / "apps" / package
        app_dir.mkdir(parents=True)
        (app_dir / "app.toml").write_text(manifest)
        (app_dir / "icon.svg").write_text(_ICON)
    return worktree


def _dump_registry(apps: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for app in apps:
        lines.append("[[apps]]")
        for key, value in app.items():
            rendered = (
                json.dumps(value) if isinstance(value, str) else str(value).lower()
            )
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


class _RecordingRunner(mod.Runner):
    """Records every shared-script invocation and files the state the real ``up`` would."""

    def __init__(
        self, repo_root: Path, next_port: int = 40000, failing_names: Sequence[str] = ()
    ) -> None:
        self.repo_root = repo_root
        self.calls: list[list[str]] = []
        self.next_port = next_port
        self.failing_names = set(failing_names)

    def run(self, argv: Sequence[str], cwd: Path) -> int:
        argv_list = list(argv)
        self.calls.append(argv_list)
        verb = argv_list[2]
        name = argv_list[argv_list.index("--name") + 1]
        if name in self.failing_names:
            return 1
        state_path = (
            self.repo_root / mod.INSTANCES_ROOT / name / mod.INSTANCE_STATE_FILENAME
        )
        if verb == "up":
            state_path.parent.mkdir(parents=True, exist_ok=True)
            self.next_port += 1
            state_path.write_text(
                json.dumps(
                    {
                        "cwd": argv_list[argv_list.index("--cwd") + 1],
                        "inner_port": self.next_port,
                    }
                )
            )
        elif verb == "down" and state_path.exists():
            state_path.unlink()
            state_path.parent.rmdir()
        return 0

    def ups(self) -> list[str]:
        return [
            call[call.index("--name") + 1] for call in self.calls if call[2] == "up"
        ]

    def up_argv(self, name: str) -> list[str]:
        return next(
            call
            for call in self.calls
            if call[2] == "up" and call[call.index("--name") + 1] == name
        )


def _flag_values(argv: Sequence[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, part in enumerate(argv) if part == flag]


def _launch(argv: Sequence[str]) -> list[str]:
    return list(argv[list(argv).index("--") + 1 :])


def test_up_hands_the_manifests_table_to_the_shared_script_with_its_own_placeholders_filled(
    tmp_path: Path,
) -> None:
    worktree = _write_worktree(tmp_path)
    runner = _RecordingRunner(tmp_path)

    code = mod.up(
        "chat",
        worktree,
        tmp_path,
        instance_key="agent-1",
        runner=runner,
        dump_registry=_dump_registry,
    )

    assert code == 0
    argv = runner.up_argv("chat-preview")
    assert argv[:3] == [sys.executable, str(mod._SHARED_SERVE_SCRIPT), "up"]
    assert _flag_values(argv, "--cwd") == [str(worktree)]
    assert _flag_values(argv, "--port") == ["main"]
    assert _flag_values(argv, "--copy") == ["data=data/.apps/chat"]
    assert _flag_values(argv, "--health-path") == ["/api/health"]
    assert _flag_values(argv, "--service-name") == ["chat-preview-app"]
    assert _flag_values(argv, "--preview-service-name") == ["chat-preview"]
    assert _flag_values(argv, "--inner-path") == ["/agent-1"]
    # The shared script's placeholders pass through; this script's own are filled (no shell
    # preview is up, so the nudge target is empty).
    assert _flag_values(argv, "--env") == [
        "CHAT_PORT={port:main}",
        "CHAT_HOST={host}",
        "CHAT_DATA_DIR={copy:data}",
    ]
    assert _launch(argv) == [
        "uv",
        "run",
        "chat-app",
        "--secondary",
        "--nudge-shell-url",
        "",
    ]


def test_a_preview_that_opens_on_an_instance_needs_its_key(tmp_path: Path) -> None:
    worktree = _write_worktree(tmp_path)
    with pytest.raises(mod.PreviewError, match="--instance-key"):
        mod.up(
            "chat",
            worktree,
            tmp_path,
            runner=_RecordingRunner(tmp_path),
            dump_registry=_dump_registry,
        )


def test_an_app_with_no_table_previews_by_the_scaffold_convention(
    tmp_path: Path,
) -> None:
    worktree = _write_worktree(tmp_path)
    runner = _RecordingRunner(tmp_path)

    assert (
        mod.up("notes", worktree, tmp_path, runner=runner, dump_registry=_dump_registry)
        == 0
    )

    argv = runner.up_argv("notes-preview")
    assert _flag_values(argv, "--env") == [
        "NOTES_PORT={port:main}",
        "NOTES_HOST={host}",
        "NOTES_DATA_DIR={copy:data}",
    ]
    assert _flag_values(argv, "--copy") == ["data=data/.apps/notes"]
    assert _flag_values(argv, "--health-path") == ["/health"]
    assert _flag_values(argv, "--inner-path") == ["/"]
    assert _launch(argv) == ["uv", "run", "notes"]


def test_a_shell_preview_boots_its_siblings_first_and_frames_them_through_a_registry_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    runner = _RecordingRunner(tmp_path)

    # The sibling's own registration lands in the live registry when it boots; stand in for it.
    def run_and_register(argv: Sequence[str], cwd: Path) -> int:
        code = _RecordingRunner.run(runner, argv, cwd)
        if argv[2] == "up" and "chat-preview" in argv:
            live_registry.write_text(
                _dump_registry(
                    [
                        {
                            "name": "chat",
                            "url": "http://localhost:8010",
                            "label": "chat-live",
                            "instances_url": "http://localhost:8010",
                            "instances": True,
                        },
                        {
                            "name": "terminal",
                            "url": "http://localhost:7681",
                            "label": "terminal-live",
                        },
                        {
                            "name": "chat-preview-app",
                            "url": "http://localhost:40001",
                            "label": "chat-preview-x1y2",
                        },
                    ]
                )
            )
        return code

    runner.run = run_and_register  # type: ignore[method-assign]
    code = mod.up(
        "system_interface",
        worktree,
        tmp_path,
        with_apps=["chat"],
        instance_key="agent-1",
        runner=runner,
        dump_registry=_dump_registry,
    )

    assert code == 0
    assert runner.ups() == ["chat-preview", "system_interface-preview"]
    shell_argv = runner.up_argv("system_interface-preview")
    registry_copy = (
        tmp_path / mod.INSTANCES_ROOT / "system_interface-preview.registry.toml"
    )
    assert f"MINDS_APPS_FILE={registry_copy}" in _flag_values(shell_argv, "--env")
    rows = {
        row["name"]: row for row in tomllib.loads(registry_copy.read_text())["apps"]
    }
    # The chat's row points at its preview, under the preview's own origin label; the rest is live.
    assert rows["chat"]["url"] == "http://127.0.0.1:40001"
    assert rows["chat"]["instances_url"] == "http://127.0.0.1:40001"
    assert rows["chat"]["label"] == "chat-preview-x1y2"
    assert rows["terminal"] == {
        "name": "terminal",
        "url": "http://localhost:7681",
        "label": "terminal-live",
    }
    # The chat booted before any shell preview was up, so its nudge target is empty; a chat
    # previewed after the shell would name it.
    assert _launch(runner.up_argv("chat-preview"))[-1] == ""
    assert (
        mod.live_preview_url(tmp_path, "system_interface") == "http://127.0.0.1:40002"
    )
    runner.calls.clear()
    assert (
        mod.up(
            "chat",
            worktree,
            tmp_path,
            instance_key="agent-1",
            runner=runner,
            dump_registry=_dump_registry,
        )
        == 0
    )
    assert _launch(runner.up_argv("chat-preview"))[-1] == "http://127.0.0.1:40002"


def test_another_passs_preview_of_the_same_app_is_refused_and_the_same_worktrees_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _write_worktree(tmp_path)
    runner = _RecordingRunner(tmp_path)
    assert (
        mod.up("notes", worktree, tmp_path, runner=runner, dump_registry=_dump_registry)
        == 0
    )

    other_worktree = tmp_path / "other"
    assert (
        mod.up(
            "notes",
            other_worktree,
            tmp_path,
            runner=runner,
            dump_registry=_dump_registry,
        )
        == 1
    )
    assert "another pass's preview" in capsys.readouterr().err
    assert runner.ups() == ["notes-preview"]
    # A re-run from the same worktree is the normal retry path.
    assert (
        mod.up("notes", worktree, tmp_path, runner=runner, dump_registry=_dump_registry)
        == 0
    )
    assert runner.ups() == ["notes-preview", "notes-preview"]


def test_down_tears_down_the_siblings_it_booted_and_drops_the_registry_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    live_registry.write_text(
        _dump_registry(
            [{"name": "chat-preview-app", "url": "http://localhost:1", "label": "p"}]
        )
    )
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    runner = _RecordingRunner(tmp_path)
    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            runner=runner,
            dump_registry=_dump_registry,
        )
        == 0
    )
    registry_copy = (
        tmp_path / mod.INSTANCES_ROOT / "system_interface-preview.registry.toml"
    )
    assert registry_copy.exists()
    runner.calls.clear()

    assert mod.down("system_interface", tmp_path, runner=runner) == 0

    downs = [
        call[call.index("--name") + 1] for call in runner.calls if call[2] == "down"
    ]
    assert downs == ["system_interface-preview", "chat-preview"]
    assert not registry_copy.exists()
    assert mod.live_preview_url(tmp_path, "chat") is None


def test_a_failed_sibling_stops_the_boot_before_the_app_itself(tmp_path: Path) -> None:
    worktree = _write_worktree(tmp_path)
    runner = _RecordingRunner(tmp_path, failing_names=["chat-preview"])

    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            runner=runner,
            dump_registry=_dump_registry,
        )
        == 1
    )
    assert runner.ups() == ["chat-preview"]


def test_a_failed_boot_keeps_the_record_of_the_siblings_it_booted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling names live only in the preview record, so a failed boot must not drop it."""
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    live_registry.write_text(
        _dump_registry(
            [{"name": "chat-preview-app", "url": "http://localhost:1", "label": "p"}]
        )
    )
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    runner = _RecordingRunner(tmp_path, failing_names=["system_interface-preview"])

    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            runner=runner,
            dump_registry=_dump_registry,
        )
        == 1
    )
    runner.calls.clear()
    runner.failing_names.clear()

    assert mod.down("system_interface", tmp_path, runner=runner) == 0
    downs = [
        call[call.index("--name") + 1] for call in runner.calls if call[2] == "down"
    ]
    assert downs == ["system_interface-preview", "chat-preview"]


def test_main_routes_the_verbs(tmp_path: Path) -> None:
    assert mod.main(["down", "--app", "notes", "--repo-root", str(tmp_path)]) == 0
    assert (
        mod.main(
            [
                "up",
                "--app",
                "notes",
                "--worktree",
                str(tmp_path / "nowhere"),
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 1
    )
