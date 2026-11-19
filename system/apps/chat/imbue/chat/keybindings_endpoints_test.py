"""The chat's keybindings: the actions its app.toml declares, and the resolved chords layered by the profile files."""

import json
from pathlib import Path
from typing import Any

from flask.testing import FlaskClient
from workspace_layout.identity import IDENTITY_HEADER
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH

from imbue.chat.keybindings_endpoints import chat_keybinding_actions
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state


def _client() -> FlaskClient:
    return create_application(build_test_state()).test_client()


def test_the_app_toml_declares_the_send_action_with_its_default_chords() -> None:
    assert [action.model_dump(mode="json") for action in chat_keybinding_actions()] == [
        {
            "id": "send-message",
            "label": "Send the message",
            "default_chords": ["enter", "ctrl+enter", "meta+enter"],
        }
    ]


def test_me_answers_the_defaults_when_no_profile_file_exists() -> None:
    answered = _client().get("/api/keybindings/me")

    assert answered.status_code == 200
    assert answered.get_json() == {
        "app": "chat",
        "chords_by_action": {"send-message": ["enter", "ctrl+enter", "meta+enter"]},
    }


def test_me_resolves_the_owners_snapshot_value_but_never_a_visitors(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The routes resolve against the working directory's profile files (cwd is
    # the workspace root in production), so the test stages one.
    monkeypatch.chdir(tmp_path)
    snapshot_path = tmp_path / SNAPSHOT_RELATIVE_PATH
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "snapshot_format": 1,
                "account_id": "account-1",
                "owner_user_id": "owner-1",
                "written_at": "2026-10-28T00:00:00Z",
                "written_by": "web",
                "keybindings": {"chat": {"send-message": ["alt+enter"]}},
            }
        ),
        encoding="utf-8",
    )
    client = _client()

    owner = client.get("/api/keybindings/me")
    assert owner.get_json()["chords_by_action"] == {"send-message": ["alt+enter"]}

    visitor = client.get(
        "/api/keybindings/me",
        headers={IDENTITY_HEADER: '{"owner": false, "user_id": "visitor-1"}'},
    )
    assert visitor.get_json()["chords_by_action"] == {"send-message": ["enter", "ctrl+enter", "meta+enter"]}
