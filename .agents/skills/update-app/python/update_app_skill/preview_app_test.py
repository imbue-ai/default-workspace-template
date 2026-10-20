"""Tests for ``preview_app.py``.

Run via: ``uv run --no-sync pytest .agents/skills/update-app/python/update_app_skill/preview_app_test.py``

A recording ``Instances`` stands in for the shared ``serve_isolated_instance``: it
records each call and files the instance state the real ``up`` would, so the
resolution of a manifest's preview table, the registry copy, the one-pass guard, and
the sibling order are all asserted on the exact boots this script asks for.
``test_preview_app_lifecycle.py`` runs the real shared substrate instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from update_app_skill import preview_app as mod

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'

_CHAT_MANIFEST = """
name = "chat"
display_name = "Chat"
icon = "icon.svg"
critical = true
priority = "chat"

[[launch_paths]]
id = "new"
label = "New Chat"
path = "/new"

[preview]
command = ["chat-app", "--secondary"]
env = {CHAT_PORT = "{port:main}", CHAT_HOST = "{host}", CHAT_DATA_DIR = "{copy:data}"}
copies = {data = "data/.apps/chat"}
health_path = "/api/health"
open_path = "/?chat={key}"
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


class _RecordingInstances(mod.Instances):
    """Records every boot and teardown, and files the state the real ``up`` would."""

    def __init__(
        self, repo_root: Path, next_port: int = 40000, failing_names: Sequence[str] = ()
    ) -> None:
        self.repo_root = repo_root
        self.specs: list[mod.InstanceSpec] = []
        self.downs: list[str] = []
        self.next_port = next_port
        self.failing_names = set(failing_names)

    def _state_path(self, name: str) -> Path:
        return self.repo_root / mod.INSTANCES_ROOT / name / mod.INSTANCE_STATE_FILENAME

    def up(self, spec: mod.InstanceSpec, repo_root: Path) -> int:
        self.specs.append(spec)
        if spec.name in self.failing_names:
            return 1
        state_path = self._state_path(spec.name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.next_port += 1
        state_path.write_text(
            json.dumps({"cwd": str(spec.cwd), "inner_port": self.next_port})
        )
        return 0

    def down(self, name: str, repo_root: Path) -> int:
        self.downs.append(name)
        if name in self.failing_names:
            return 1
        state_path = self._state_path(name)
        if state_path.exists():
            state_path.unlink()
            state_path.parent.rmdir()
        return 0

    def ups(self) -> list[str]:
        return [spec.name for spec in self.specs]

    def up_spec(self, name: str) -> mod.InstanceSpec:
        return next(spec for spec in self.specs if spec.name == name)


def _shell_registry_copy(tmp_path: Path) -> Path:
    return mod._registry_copy_path(tmp_path, "system_interface")


def _shell_registry_rows(tmp_path: Path) -> dict[str, dict[str, object]]:
    rows = tomllib.loads(_shell_registry_copy(tmp_path).read_text())["apps"]
    return {row["name"]: row for row in rows}


def test_up_hands_the_manifests_table_to_the_shared_script_with_its_own_placeholders_filled(
    tmp_path: Path,
) -> None:
    worktree = _write_worktree(tmp_path)
    instances = _RecordingInstances(tmp_path)

    code = mod.up(
        "chat",
        worktree,
        tmp_path,
        instance_key="agent-1",
        instances=instances,
        dump_registry=_dump_registry,
    )

    assert code == 0
    # The shared substrate's placeholders pass through untouched.
    assert instances.up_spec("chat-preview") == mod.InstanceSpec(
        name="chat-preview",
        command=[*mod.LAUNCHER, "chat-app", "--secondary"],
        cwd=worktree,
        port_env_by_name={"main": None},
        env_overrides={
            "CHAT_PORT": "{port:main}",
            "CHAT_HOST": "{host}",
            "CHAT_DATA_DIR": "{copy:data}",
        },
        copy_sources={"data": "data/.apps/chat"},
        health_path="/api/health",
        service_name="chat-preview-app",
        preview_service_name="chat-preview",
        preview_title="Chat (worktree)",
        inner_path="/?chat=agent-1",
    )


def test_the_launcher_syncs_the_worktree_frozen_then_becomes_the_app(
    tmp_path: Path,
) -> None:
    """The app replaces the launcher's shell, so no uv process stays its parent, and it
    runs from the worktree's own venv after a sync that never relocks."""
    worktree = tmp_path / "worktree"
    (worktree / ".venv" / "bin").mkdir(parents=True)
    app = worktree / ".venv" / "bin" / "app"
    app.write_text('#!/bin/sh\necho "$$ $VIRTUAL_ENV $*"\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(f'#!/bin/sh\necho "$*" > {tmp_path / "uv-args"}\n')
    for script in (app, uv):
        script.chmod(0o755)

    process = subprocess.Popen(
        [*mod.LAUNCHER, "app", "--port", "1"],
        cwd=worktree,
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout, _ = process.communicate(timeout=30)

    assert process.returncode == 0
    assert (tmp_path / "uv-args").read_text().split() == [
        "sync",
        "--all-packages",
        "--frozen",
    ]
    assert stdout.split() == [str(process.pid), str(worktree / ".venv"), "--port", "1"]


def test_a_preview_that_opens_on_an_instance_needs_its_key(tmp_path: Path) -> None:
    worktree = _write_worktree(tmp_path)
    with pytest.raises(mod.PreviewError, match="--instance-key"):
        mod.up(
            "chat",
            worktree,
            tmp_path,
            instances=_RecordingInstances(tmp_path),
            dump_registry=_dump_registry,
        )


def test_an_app_with_no_table_previews_by_the_scaffold_convention(
    tmp_path: Path,
) -> None:
    worktree = _write_worktree(tmp_path)
    instances = _RecordingInstances(tmp_path)

    assert (
        mod.up(
            "notes",
            worktree,
            tmp_path,
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )

    spec = instances.up_spec("notes-preview")
    assert spec.env_overrides == {
        "NOTES_PORT": "{port:main}",
        "NOTES_HOST": "{host}",
        "NOTES_DATA_DIR": "{copy:data}",
    }
    assert spec.copy_sources == {"data": "data/.apps/notes"}
    assert spec.health_path == "/health"
    assert spec.inner_path == "/"
    assert spec.command == [*mod.LAUNCHER, "notes"]


def test_an_app_that_runs_under_secret_files_previews_under_the_same_wrapper(
    tmp_path: Path,
) -> None:
    """The live program reads its declared secrets only through with_secrets.py, so the
    preview must too, from the live repo's data/ (an editing worktree has none)."""
    worktree = _write_worktree(tmp_path)
    (worktree / "system" / "apps" / "notes" / "app.toml").write_text(
        _NOTES_MANIFEST
        + '\n[[secrets]]\nfile = "notes-api"\nvariables = ["NOTES_API_KEY"]\n'
        + '\n[[secrets]]\nfile = "notes-mail"\nvariables = ["NOTES_MAIL_TOKEN"]\n'
    )
    instances = _RecordingInstances(tmp_path)

    assert (
        mod.up(
            "notes",
            worktree,
            tmp_path,
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )

    wrapper = str(tmp_path / "system" / "scripts" / "with_secrets.py")
    assert instances.up_spec("notes-preview").command == [
        *mod.LAUNCHER,
        "python3",
        wrapper,
        str(tmp_path / "data" / ".secrets" / "notes-api.env"),
        "--",
        "python3",
        wrapper,
        str(tmp_path / "data" / ".secrets" / "notes-mail.env"),
        "--",
        "notes",
    ]


def test_a_shell_preview_boots_its_siblings_first_and_frames_them_through_a_registry_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    instances = _RecordingInstances(tmp_path)

    # The sibling's own registration lands in the live registry when it boots; stand in for it.
    def up_and_register(spec: mod.InstanceSpec, repo_root: Path) -> int:
        code = _RecordingInstances.up(instances, spec, repo_root)
        if spec.name == "chat-preview":
            live_registry.write_text(
                _dump_registry(
                    [
                        {
                            "name": "chat",
                            "url": "http://localhost:8010",
                            "label": "chat-live",
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

    instances.up = up_and_register  # type: ignore[method-assign]
    code = mod.up(
        "system_interface",
        worktree,
        tmp_path,
        with_apps=["chat"],
        instance_key="agent-1",
        instances=instances,
        dump_registry=_dump_registry,
    )

    assert code == 0
    assert instances.ups() == ["chat-preview", "system_interface-preview"]
    shell_spec = instances.up_spec("system_interface-preview")
    assert shell_spec.env_overrides["MINDS_APPS_FILE"] == str(
        _shell_registry_copy(tmp_path)
    )
    rows = _shell_registry_rows(tmp_path)
    # The chat's row points at its preview, under the preview's own origin label; the rest is live.
    assert rows["chat"]["url"] == "http://127.0.0.1:40001"
    assert rows["chat"]["label"] == "chat-preview-x1y2"
    assert rows["terminal"] == {
        "name": "terminal",
        "url": "http://localhost:7681",
        "label": "terminal-live",
    }
    assert (
        mod.live_preview_url(tmp_path, "system_interface") == "http://127.0.0.1:40002"
    )


def test_another_passs_preview_of_the_same_app_is_refused_and_the_same_worktrees_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _write_worktree(tmp_path)
    instances = _RecordingInstances(tmp_path)
    assert (
        mod.up(
            "notes",
            worktree,
            tmp_path,
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )

    other_worktree = tmp_path / "other"
    assert (
        mod.up(
            "notes",
            other_worktree,
            tmp_path,
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 1
    )
    assert "another pass's preview" in capsys.readouterr().err
    assert instances.ups() == ["notes-preview"]
    # A re-run from the same worktree is the normal retry path.
    assert (
        mod.up(
            "notes",
            worktree,
            tmp_path,
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )
    assert instances.ups() == ["notes-preview", "notes-preview"]


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
    instances = _RecordingInstances(tmp_path)
    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )
    registry_copy = _shell_registry_copy(tmp_path)
    assert registry_copy.exists()
    instances.downs.clear()

    assert mod.down("system_interface", tmp_path, instances=instances) == 0

    assert instances.downs == ["system_interface-preview", "chat-preview"]
    assert not registry_copy.exists()
    assert mod.live_preview_url(tmp_path, "chat") is None


def test_a_re_up_without_siblings_still_tears_down_the_ones_an_earlier_up_booted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The earlier ``--with`` sibling is still running, and the record is ``down``'s only way to it."""
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    live_registry.write_text(
        _dump_registry(
            [{"name": "chat-preview-app", "url": "http://localhost:1", "label": "p"}]
        )
    )
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    instances = _RecordingInstances(tmp_path)
    for with_apps in (["chat"], []):
        assert (
            mod.up(
                "system_interface",
                worktree,
                tmp_path,
                with_apps=with_apps,
                instance_key="agent-1",
                instances=instances,
                dump_registry=_dump_registry,
            )
            == 0
        )
    instances.downs.clear()

    assert mod.down("system_interface", tmp_path, instances=instances) == 0

    assert instances.downs == ["system_interface-preview", "chat-preview"]


def test_a_failed_sibling_stops_the_boot_before_the_app_itself(tmp_path: Path) -> None:
    worktree = _write_worktree(tmp_path)
    instances = _RecordingInstances(tmp_path, failing_names=["chat-preview"])

    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 1
    )
    assert instances.ups() == ["chat-preview"]


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
    instances = _RecordingInstances(
        tmp_path, failing_names=["system_interface-preview"]
    )

    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 1
    )
    instances.downs.clear()
    instances.failing_names.clear()

    assert mod.down("system_interface", tmp_path, instances=instances) == 0
    assert instances.downs == ["system_interface-preview", "chat-preview"]


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


def _up_shell_with_chat(
    tmp_path: Path, worktree: Path, instances: _RecordingInstances
) -> None:
    assert (
        mod.up(
            "system_interface",
            worktree,
            tmp_path,
            with_apps=["chat"],
            instance_key="agent-1",
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )


def test_re_upping_a_framed_sibling_on_its_own_moves_the_shells_registry_copy_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell preview reads its siblings' URLs from a copy written when it booted and watches
    that file, so a chat re-upped alone (new ports) has to be written into it, or the shell
    keeps framing a port nothing listens on."""
    worktree = _write_worktree(tmp_path)
    live_registry = tmp_path / "apps.toml"
    live_registry.write_text(
        _dump_registry(
            [
                {"name": "chat", "url": "http://localhost:8010", "label": "chat-live"},
                {
                    "name": "chat-preview-app",
                    "url": "http://localhost:1",
                    "label": "chat-preview-x1y2",
                },
            ]
        )
    )
    monkeypatch.setenv("MINDS_APPS_FILE", str(live_registry))
    instances = _RecordingInstances(tmp_path)
    _up_shell_with_chat(tmp_path, worktree, instances)
    first_chat_url = mod.live_preview_url(tmp_path, "chat")
    assert _shell_registry_rows(tmp_path)["chat"]["url"] == first_chat_url

    assert (
        mod.up(
            "chat",
            worktree,
            tmp_path,
            instance_key="agent-1",
            instances=instances,
            dump_registry=_dump_registry,
        )
        == 0
    )

    second_chat_url = mod.live_preview_url(tmp_path, "chat")
    assert second_chat_url != first_chat_url
    rows = _shell_registry_rows(tmp_path)
    assert rows["chat"]["url"] == second_chat_url
    assert rows["chat"]["label"] == "chat-preview-x1y2"

    # Taken down on its own, the chat's row goes back to the live chat.
    assert (
        mod.down("chat", tmp_path, instances=instances, dump_registry=_dump_registry)
        == 0
    )
    rows = _shell_registry_rows(tmp_path)
    assert rows["chat"] == {
        "name": "chat",
        "url": "http://localhost:8010",
        "label": "chat-live",
    }
    # The shell's own record still names the chat, so a later ``down`` of the shell is unchanged.
    assert mod._recorded_siblings(tmp_path, "system_interface") == ["chat"]
