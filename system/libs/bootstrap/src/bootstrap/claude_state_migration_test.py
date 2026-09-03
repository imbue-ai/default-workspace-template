"""Unit tests for the legacy (root-homed) claude state migration."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from bootstrap.claude_state_migration import REPLACED_CLAUDE_JSON_NAME
from bootstrap.claude_state_migration import migrate_legacy_claude_state


def _make_stranded_legacy_home(legacy_home: Path) -> None:
    """Lay down the state shape observed on a real stranded pre-layout workspace.

    ``/root/.claude`` with transcripts, credentials, settings, and plugins,
    plus the global ``/root/.claude.json`` beside it.
    """
    config_dir = legacy_home / ".claude"
    project_dir = config_dir / "projects" / "-mnt-lima-mngr-data-workspace"
    project_dir.mkdir(parents=True)
    (project_dir / f"{uuid4().hex}.jsonl").write_text('{"type": "user"}\n')
    (config_dir / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    (config_dir / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "sk-old"}}')
    (config_dir / "plugins").mkdir()
    (config_dir / "plugins" / "config.json").write_text("{}")
    (config_dir / "history.jsonl").write_text('{"display": "hi"}\n')
    (legacy_home / ".claude.json").write_text(json.dumps({"firstStartTime": "old", "customApiKeyResponses": {}}))


def _make_fresh_current_home(current_home: Path) -> None:
    """The destination as the post-update services leave it: fresh and empty."""
    (current_home / ".claude" / "backups").mkdir(parents=True)
    (current_home / ".claude.json").write_text(json.dumps({"firstStartTime": "new"}))


def test_migrates_transcripts_credentials_settings_plugins_and_claude_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home" / "user"
    current_home.mkdir(parents=True)
    _make_stranded_legacy_home(legacy_home)
    _make_fresh_current_home(current_home)

    assert migrate_legacy_claude_state(legacy_home, current_home) is True

    config_dir = current_home / ".claude"
    transcripts = list((config_dir / "projects").glob("*/*.jsonl"))
    assert len(transcripts) == 1
    assert transcripts[0].read_text() == '{"type": "user"}\n'
    assert (config_dir / ".credentials.json").read_text() == '{"claudeAiOauth": {}}'
    assert json.loads((config_dir / "settings.json").read_text())["env"]["ANTHROPIC_API_KEY"] == "sk-old"
    assert (config_dir / "plugins" / "config.json").is_file()
    assert (config_dir / "history.jsonl").is_file()
    # The legacy global config replaces the fresh one, which is preserved aside.
    assert json.loads((current_home / ".claude.json").read_text())["firstStartTime"] == "old"
    assert json.loads((current_home / REPLACED_CLAUDE_JSON_NAME).read_text())["firstStartTime"] == "new"
    # The migrated entries no longer exist at the legacy location.
    assert not (legacy_home / ".claude" / "projects").exists()
    assert not (legacy_home / ".claude.json").exists()


def test_second_run_is_a_noop_after_a_successful_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    current_home.mkdir()
    _make_stranded_legacy_home(legacy_home)

    assert migrate_legacy_claude_state(legacy_home, current_home) is True
    assert migrate_legacy_claude_state(legacy_home, current_home) is False


def test_noop_when_legacy_and_current_home_are_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _make_stranded_legacy_home(tmp_path)

    assert migrate_legacy_claude_state(tmp_path, tmp_path) is False
    # Nothing moved: the state is exactly where it was.
    assert (tmp_path / ".claude.json").is_file()
    assert next((tmp_path / ".claude" / "projects").glob("*/*.jsonl"), None) is not None


def test_noop_when_legacy_holds_no_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    current_home.mkdir()
    # A toolchain-created .claude with no transcripts and no credentials (e.g.
    # an empty projects dir plus a plugins cache) is not user state.
    (legacy_home / ".claude" / "projects").mkdir(parents=True)
    (legacy_home / ".claude" / "plugins").mkdir()

    assert migrate_legacy_claude_state(legacy_home, current_home) is False
    assert not (current_home / ".claude").exists()


def test_migrates_credentials_only_state_without_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    current_home.mkdir()
    (legacy_home / ".claude").mkdir(parents=True)
    (legacy_home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {}}')

    assert migrate_legacy_claude_state(legacy_home, current_home) is True
    assert (current_home / ".claude" / ".credentials.json").read_text() == '{"claudeAiOauth": {}}'


def test_refuses_when_current_home_already_holds_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    _make_stranded_legacy_home(legacy_home)
    new_project_dir = current_home / ".claude" / "projects" / "-home-user-workspace"
    new_project_dir.mkdir(parents=True)
    (new_project_dir / f"{uuid4().hex}.jsonl").write_text('{"type": "user"}\n')
    (current_home / ".claude.json").write_text('{"firstStartTime": "new"}')

    assert migrate_legacy_claude_state(legacy_home, current_home) is False
    # Both sides untouched: the lived-in new home is never clobbered.
    assert next((legacy_home / ".claude" / "projects").glob("*/*.jsonl"), None) is not None
    assert (current_home / ".claude.json").read_text() == '{"firstStartTime": "new"}'
    assert not (current_home / ".claude" / ".credentials.json").exists()


def test_colliding_top_level_entries_stay_at_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    _make_stranded_legacy_home(legacy_home)
    (legacy_home / ".claude" / "backups").mkdir()
    (legacy_home / ".claude" / "backups" / "old.txt").write_text("old")
    _make_fresh_current_home(current_home)
    (current_home / ".claude" / "backups" / "new.txt").write_text("new")

    assert migrate_legacy_claude_state(legacy_home, current_home) is True

    # The destination's colliding dir is kept as-is; the legacy copy stays behind.
    assert (current_home / ".claude" / "backups" / "new.txt").read_text() == "new"
    assert not (current_home / ".claude" / "backups" / "old.txt").exists()
    assert (legacy_home / ".claude" / "backups" / "old.txt").read_text() == "old"
    # Non-colliding state still migrated.
    assert (current_home / ".claude" / ".credentials.json").is_file()


def test_returns_false_when_every_entry_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    (legacy_home / ".claude").mkdir(parents=True)
    (legacy_home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"old": true}}')
    (current_home / ".claude").mkdir(parents=True)
    (current_home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"new": true}}')

    assert migrate_legacy_claude_state(legacy_home, current_home) is False
    # Both sides untouched: the destination copy wins, the legacy one stays behind.
    assert (current_home / ".claude" / ".credentials.json").read_text() == '{"claudeAiOauth": {"new": true}}'
    assert (legacy_home / ".claude" / ".credentials.json").read_text() == '{"claudeAiOauth": {"old": true}}'


def test_migrates_config_dir_contents_when_legacy_claude_json_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    current_home.mkdir()
    _make_stranded_legacy_home(legacy_home)
    (legacy_home / ".claude.json").unlink()

    assert migrate_legacy_claude_state(legacy_home, current_home) is True
    assert next((current_home / ".claude" / "projects").glob("*/*.jsonl"), None) is not None
    assert not (current_home / ".claude.json").exists()


def test_skipped_entirely_when_claude_config_dir_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_home = tmp_path / "root"
    current_home = tmp_path / "home"
    current_home.mkdir()
    _make_stranded_legacy_home(legacy_home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))

    assert migrate_legacy_claude_state(legacy_home, current_home) is False
    assert (legacy_home / ".claude.json").is_file()
    assert not (current_home / ".claude").exists()
