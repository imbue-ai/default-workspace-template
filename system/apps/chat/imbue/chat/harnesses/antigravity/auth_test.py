"""agy's key mode: the two files it writes, and what it refuses to overwrite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imbue.chat.harnesses.antigravity.auth import AntigravitySettingsError
from imbue.chat.harnesses.antigravity.auth import gemini_credential_paths
from imbue.chat.harnesses.antigravity.auth import gemini_env_path
from imbue.chat.harnesses.antigravity.auth import has_gemini_api_key
from imbue.chat.harnesses.antigravity.auth import read_gemini_api_key
from imbue.chat.harnesses.antigravity.auth import write_gemini_api_key
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path


def _settings_path(account_dir: Path) -> Path:
    return get_antigravity_settings_path(account_dir)


def _write_settings(account_dir: Path, content: str) -> Path:
    path = _settings_path(account_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_the_mode_merges_over_the_settings_agy_already_wrote(tmp_path: Path) -> None:
    """agy writes this file itself, running under `HOME=<account folder>` during a browser sign-in
    on this same lane; nothing else puts anything in it (`binding.seed_account` seeds agy with
    nothing, and the agent type's `settings_overrides` reach the PER-AGENT settings.json instead).
    Replacing it would discard whatever that sign-in left behind."""
    _write_settings(tmp_path, json.dumps({"enableTelemetry": False, "showTips": False}))

    write_gemini_api_key(tmp_path, "AIzaSyValid")

    assert json.loads(_settings_path(tmp_path).read_text()) == {
        "enableTelemetry": False,
        "showTips": False,
        "modelProvider": "gemini",
    }


@pytest.mark.parametrize("content", ("", "   \n"), ids=("absent", "empty"))
def test_a_file_with_nothing_in_it_starts_from_an_empty_object(tmp_path: Path, content: str) -> None:
    if content:
        _write_settings(tmp_path, content)

    write_gemini_api_key(tmp_path, "AIzaSyValid")

    assert json.loads(_settings_path(tmp_path).read_text()) == {"modelProvider": "gemini"}


@pytest.mark.parametrize(
    "content",
    ('{"enableTips": tru', '["not", "an", "object"]'),
    ids=("malformed", "not an object"),
)
def test_settings_this_cannot_read_are_raised_on_rather_than_overwritten(tmp_path: Path, content: str) -> None:
    """Whatever put unreadable content there put it there on purpose -- a future agy schema, or a
    hand edit. Writing over it would destroy the only copy."""
    path = _write_settings(tmp_path, content)

    with pytest.raises(AntigravitySettingsError):
        write_gemini_api_key(tmp_path, "AIzaSyValid")

    assert path.read_text() == content
    # The key file is written first, so a raise here leaves the account half-made. What clears it
    # is the flow's rollback over `gemini_credential_paths`, which is why that covers both files.
    assert gemini_env_path(tmp_path).is_file()


def test_both_credential_paths_sit_inside_the_account_folder(tmp_path: Path) -> None:
    """The re-auth backup keys a parked file by its path relative to the account folder, and raises
    on a path from anywhere else."""
    for path in gemini_credential_paths(tmp_path):
        assert path.relative_to(tmp_path)


@pytest.mark.parametrize(
    "content",
    (None, "# no key here\n", "GEMINI_PROJECT=abc\n"),
    ids=("no file", "comment only", "another variable"),
)
def test_an_account_with_no_key_reads_as_none(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        gemini_env_path(tmp_path).write_text(content)

    assert read_gemini_api_key(tmp_path) is None
    # The file's presence is the marker for key mode, whatever it holds: `signed_in` reads it that
    # way so a half-written file cannot look healthy.
    assert has_gemini_api_key(tmp_path) is (content is not None)
