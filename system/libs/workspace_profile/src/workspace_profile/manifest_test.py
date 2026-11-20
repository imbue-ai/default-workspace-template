from pathlib import Path

import pytest
from workspace_layout.identity import RequestIdentity

from workspace_profile.errors import InvalidKeybindingDeclarationError
from workspace_profile.manifest import (
    load_app_keybinding_actions,
    resolve_keybindings_me_response,
)
from workspace_profile.overrides import write_user_override_values


def _write_app_toml(tmp_path: Path, keybindings: str, app_name: str = "notes") -> Path:
    """An app.toml declaring ``keybindings``; a workspace-wide action needs the shell's name, the only app the
    manifest lets declare one."""
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text(
        f'name = "{app_name}"\ndisplay_name = "Notes"\ninternal = true\n\n{keybindings}',
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


@pytest.mark.parametrize(
    ("keybinding", "match"),
    [
        pytest.param(
            '[[keybindings]]\nid = "new-folder"\nlabel = "New folder"\ndefault_chords = ["meta+shift+n"]\n',
            "new-folder: meta\\+shift\\+n",
            id="off-grammar",
        ),
        pytest.param(
            '[[keybindings]]\nid = "reboot"\nlabel = "Reboot"\ndefault_chords = ["alt+meta+delete"]\n',
            "reboot: alt\\+meta\\+delete",
            id="refused",
        ),
        pytest.param(
            '[[keybindings]]\nid = "launcher"\nlabel = "Launcher"\ndefault_chords = ["shift+k"]\nscope = "workspace"\n',
            "launcher: shift\\+k",
            id="workspace-wide-without-a-real-modifier",
        ),
        pytest.param(
            '[[keybindings]]\nid = "jump"\nlabel = "Jump"\ndefault_chords = ['
            + ", ".join(f'"meta+{n}"' for n in range(10))
            + ', "meta+a", "meta+b", "meta+c", "meta+d", "meta+e", "meta+f", "meta+g"]\n',
            "jump: more than 16 chords",
            id="too-many",
        ),
    ],
)
def test_a_default_chord_an_action_may_not_have_refuses_to_load(
    tmp_path: Path, keybinding: str, match: str
) -> None:
    manifest_path = _write_app_toml(tmp_path, keybinding, app_name="system_interface")

    with pytest.raises(InvalidKeybindingDeclarationError, match=match):
        load_app_keybinding_actions(manifest_path)


def test_a_workspace_wide_default_holds_a_real_modifier_or_is_a_function_key(tmp_path: Path) -> None:
    manifest_path = _write_app_toml(
        tmp_path,
        '[[keybindings]]\nid = "launcher"\nlabel = "Launcher"\ndefault_chords = ["meta+k", "f6", "alt+shift+k"]\n'
        'scope = "workspace"\n',
        app_name="system_interface",
    )

    (action,) = load_app_keybinding_actions(manifest_path)
    assert action.default_chords == ("meta+k", "f6", "alt+shift+k")


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
