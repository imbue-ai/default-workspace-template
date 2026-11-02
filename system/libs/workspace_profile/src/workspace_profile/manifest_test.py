from pathlib import Path

import pytest
from workspace_layout.identity import RequestIdentity

from workspace_profile.errors import InvalidKeybindingDeclarationError
from workspace_profile.manifest import (
    load_app_keybinding_actions,
    resolve_keybindings_me_response,
)
from workspace_profile.overrides import write_user_override_values


def _write_app_toml(tmp_path: Path, keybindings: str) -> Path:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text(
        f'name = "notes"\ndisplay_name = "Notes"\ninternal = true\n\n{keybindings}',
        encoding="utf-8",
    )
    return manifest_path


def test_the_actions_an_app_toml_declares_load_in_display_order(tmp_path: Path) -> None:
    manifest_path = _write_app_toml(
        tmp_path,
        '[[keybindings]]\nid = "save"\nlabel = "Save"\ndefault_chords = ["ctrl+s", "meta+s"]\n\n'
        '[[keybindings]]\nid = "close"\nlabel = "Close"\n',
    )

    actions = load_app_keybinding_actions(manifest_path)

    assert [(action.id, action.default_chords) for action in actions] == [
        ("save", ("ctrl+s", "meta+s")),
        ("close", ()),
    ]


def test_a_default_chord_off_the_grammar_refuses_to_load(tmp_path: Path) -> None:
    manifest_path = _write_app_toml(
        tmp_path,
        '[[keybindings]]\nid = "new-folder"\nlabel = "New folder"\ndefault_chords = ["meta+shift+n"]\n',
    )

    with pytest.raises(
        InvalidKeybindingDeclarationError, match="new-folder: meta\\+shift\\+n"
    ):
        load_app_keybinding_actions(manifest_path)


def test_the_me_answer_layers_the_users_override_over_the_declared_defaults(
    tmp_path: Path,
) -> None:
    actions = load_app_keybinding_actions(
        _write_app_toml(
            tmp_path,
            '[[keybindings]]\nid = "save"\nlabel = "Save"\ndefault_chords = ["ctrl+s"]\n\n'
            '[[keybindings]]\nid = "close"\nlabel = "Close"\ndefault_chords = ["escape"]\n',
        )
    )
    workspace_root = tmp_path / "workspace"
    write_user_override_values(
        workspace_root, "visitor-1", {"keybindings": {"notes": {"save": ["f5"]}}}
    )

    answer = resolve_keybindings_me_response(
        "notes",
        actions,
        snapshot=None,
        identity=RequestIdentity(
            owner=False, user_id="visitor-1", email="v@example.com"
        ),
        workspace_root=workspace_root,
    )

    assert answer.app == "notes"
    assert answer.chords_by_action == {"save": ["f5"], "close": ["escape"]}
