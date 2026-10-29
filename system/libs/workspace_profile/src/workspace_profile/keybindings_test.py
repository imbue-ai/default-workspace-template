import json
from pathlib import Path

import pytest

from workspace_profile.errors import InvalidChordError, ProfileWriteError
from workspace_profile.identity import ANONYMOUS_OWNER, RequestIdentity
from workspace_profile.keybindings import (
    describe_keybindings_value_problem,
    is_canonical_chord,
    keybinding_assignment,
    normalize_chord,
    parse_keybindings_value,
    resolve_app_keybindings,
    with_keybinding_assignment,
    without_keybinding_assignment,
)
from workspace_profile.overrides import (
    UNSENT_CHANGES_RELATIVE_PATH,
    load_unsent_change_values,
    load_user_override_values,
    write_unsent_change_values,
    write_user_override_values,
)
from workspace_profile.snapshot import WorkspaceProfileSnapshot

_VISITOR = RequestIdentity(owner=False, user_id="visitor-1", email="v@example.com")


def _snapshot(keybindings: dict[str, object]) -> WorkspaceProfileSnapshot:
    return WorkspaceProfileSnapshot(
        account_id="account-1",
        owner_user_id="owner-1",
        value_by_key={"keybindings": keybindings},
    )


@pytest.mark.parametrize(
    "chord",
    ["a", "7", "[", "enter", "f11", "ctrl+a", "ctrl+shift+p", "alt+arrowleft", "ctrl+alt+shift+meta+z", "meta+plus"],
)
def test_canonical_chords_are_accepted(chord: str) -> None:
    assert is_canonical_chord(chord) is True


@pytest.mark.parametrize(
    "chord",
    ["", "A", "Ctrl+a", "cmd+a", "shift+ctrl+a", "ctrl+ctrl+a", "ctrl+", "+", "ctrl++", "esc", "ctrl", "a+b"],
    ids=[
        "empty",
        "uppercase-key",
        "uppercase-modifier",
        "unnormalized-cmd",
        "out-of-order-modifiers",
        "repeated-modifier",
        "no-key",
        "bare-separator",
        "plus-as-literal-key",
        "unknown-name",
        "modifier-as-key",
        "two-keys",
    ],
)
def test_non_canonical_chords_are_refused(chord: str) -> None:
    assert is_canonical_chord(chord) is False


@pytest.mark.parametrize(
    ("spelled", "canonical"),
    [
        ("Ctrl+Shift+P", "ctrl+shift+p"),
        ("cmd+Enter", "meta+enter"),
        ("shift+ctrl+a", "ctrl+shift+a"),
        ("Esc", "escape"),
        ("Return", "enter"),
        ("ctrl++", "ctrl+plus"),
        ("F2", "f2"),
        ("alt+ ", "alt+space"),
        ("enter", "enter"),
    ],
)
def test_normalize_chord_canonicalizes_recorder_spellings(spelled: str, canonical: str) -> None:
    normalized = normalize_chord(spelled)
    assert normalized == canonical
    assert is_canonical_chord(normalized)


@pytest.mark.parametrize("spelled", ["", "ctrl+", "banana+a", "ctrl+ctrl+a", "unknownkey"])
def test_normalize_chord_refuses_what_it_cannot_spell(spelled: str) -> None:
    with pytest.raises(InvalidChordError):
        normalize_chord(spelled)


def test_a_valid_keybindings_value_has_no_problem() -> None:
    value = {"files": {"rename": ["f2"], "delete": []}, "chat": {"send-message": ["enter", "ctrl+enter"]}}
    assert describe_keybindings_value_problem(value) is None
    assert describe_keybindings_value_problem({}) is None


@pytest.mark.parametrize(
    ("value", "problem_fragment"),
    [
        ("enter", "must be an object"),
        ({"Files": {}}, "not an app name"),
        ({"files": ["f2"]}, "object of actions"),
        ({"files": {"Rename!": ["f2"]}}, "not an action id"),
        ({"files": {"rename": "f2"}}, "list of chords"),
        ({"files": {"rename": ["F2"]}}, "not a canonical chord"),
    ],
    ids=["not-a-map", "bad-app-name", "no-action-level", "bad-action-id", "chords-not-a-list", "bad-chord"],
)
def test_keybindings_value_problems_are_named(value: object, problem_fragment: str) -> None:
    problem = describe_keybindings_value_problem(value)
    assert problem is not None and problem_fragment in problem


def test_parse_keybindings_value_keeps_assignments_and_drops_malformed_entries() -> None:
    parsed = parse_keybindings_value(
        {"chat": {"send-message": ["enter", 7], "bad": "enter"}, "broken": ["x"], 3: {}}
    )
    assert parsed == {"chat": {"send-message": ["enter"]}}
    assert parse_keybindings_value("enter") == {}
    assert parse_keybindings_value(None) == {}


def test_resolution_order_for_the_owner(tmp_path: Path) -> None:
    # Default loses to the snapshot, which loses to the unsent layer, which
    # loses to the owner's own override file.
    defaults = {"send-message": ["enter"]}
    snapshot = _snapshot({"chat": {"send-message": ["ctrl+enter"]}})

    resolved = resolve_app_keybindings(
        "chat", defaults, snapshot=snapshot, identity=ANONYMOUS_OWNER, workspace_root=tmp_path
    )
    assert resolved == {"send-message": ["ctrl+enter"]}

    write_unsent_change_values(tmp_path, {"keybindings": {"chat": {"send-message": ["meta+enter"]}}})
    resolved_with_unsent = resolve_app_keybindings(
        "chat", defaults, snapshot=snapshot, identity=ANONYMOUS_OWNER, workspace_root=tmp_path
    )
    assert resolved_with_unsent == {"send-message": ["meta+enter"]}

    write_user_override_values(tmp_path, "owner-1", {"keybindings": {"chat": {"send-message": ["alt+enter"]}}})
    resolved_with_override = resolve_app_keybindings(
        "chat", defaults, snapshot=snapshot, identity=ANONYMOUS_OWNER, workspace_root=tmp_path
    )
    assert resolved_with_override == {"send-message": ["alt+enter"]}


def test_a_visitor_never_sees_the_owners_account_or_unsent_values(tmp_path: Path) -> None:
    defaults = {"send-message": ["enter"]}
    snapshot = _snapshot({"chat": {"send-message": ["ctrl+enter"]}})
    write_unsent_change_values(tmp_path, {"keybindings": {"chat": {"send-message": ["meta+enter"]}}})

    resolved = resolve_app_keybindings(
        "chat", defaults, snapshot=snapshot, identity=_VISITOR, workspace_root=tmp_path
    )
    assert resolved == {"send-message": ["enter"]}

    write_user_override_values(tmp_path, "visitor-1", {"keybindings": {"chat": {"send-message": ["f9"]}}})
    resolved_with_own = resolve_app_keybindings(
        "chat", defaults, snapshot=snapshot, identity=_VISITOR, workspace_root=tmp_path
    )
    assert resolved_with_own == {"send-message": ["f9"]}


def test_an_unknown_actions_assignment_never_leaks_into_the_resolution(tmp_path: Path) -> None:
    snapshot = _snapshot({"chat": {"future-action": ["f9"]}})
    resolved = resolve_app_keybindings(
        "chat", {"send-message": ["enter"]}, snapshot=snapshot, identity=ANONYMOUS_OWNER, workspace_root=tmp_path
    )
    assert resolved == {"send-message": ["enter"]}


def test_an_empty_assignment_unbinds_rather_than_falling_back(tmp_path: Path) -> None:
    snapshot = _snapshot({"chat": {"send-message": []}})
    resolved = resolve_app_keybindings(
        "chat", {"send-message": ["enter"]}, snapshot=snapshot, identity=ANONYMOUS_OWNER, workspace_root=tmp_path
    )
    assert resolved == {"send-message": []}


def test_assignment_helpers_round_trip_one_layer() -> None:
    layer: dict[str, object] = {"other_setting": True}
    with_one = with_keybinding_assignment(layer, "chat", "send-message", ["ctrl+enter"])
    assert keybinding_assignment(with_one, "chat", "send-message") == ["ctrl+enter"]
    assert with_one["other_setting"] is True

    without = without_keybinding_assignment(with_one, "chat", "send-message")
    assert keybinding_assignment(without, "chat", "send-message") is None
    # The emptied keybindings key is dropped entirely rather than left as {}.
    assert "keybindings" not in without
    assert without["other_setting"] is True


def test_the_writers_round_trip_through_the_loaders(tmp_path: Path) -> None:
    write_user_override_values(tmp_path, "user-1", {"keybindings": {"files": {"rename": ["f2"]}}})
    assert load_user_override_values(tmp_path, "user-1") == {"keybindings": {"files": {"rename": ["f2"]}}}

    write_unsent_change_values(tmp_path, {"keybindings": {"files": {"rename": ["f3"]}}})
    assert load_unsent_change_values(tmp_path) == {"keybindings": {"files": {"rename": ["f3"]}}}
    raw = json.loads((tmp_path / UNSENT_CHANGES_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert raw == {"keybindings": {"files": {"rename": ["f3"]}}}


def test_the_override_writer_refuses_a_path_shaped_user_id(tmp_path: Path) -> None:
    with pytest.raises(ProfileWriteError):
        write_user_override_values(tmp_path, "../escape", {"keybindings": {}})
