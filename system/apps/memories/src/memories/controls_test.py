import json
from pathlib import Path

import pytest

from memories.controls import MemoryControls
from memories.controls import MemoryHarness
from memories.controls import is_memory_on_for
from memories.controls import read_controls
from memories.controls import save_controls
from memories.errors import ControlsReadError


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "apps" / "memories" / "settings.json", tmp_path / ".claude" / "settings.local.json"


def test_no_settings_file_means_memory_is_on_for_every_chat(tmp_path: Path) -> None:
    controls = read_controls(tmp_path / "missing.json")

    assert controls == MemoryControls(is_paused=False, disabled_harnesses=())
    assert all(is_memory_on_for(controls, harness) for harness in MemoryHarness)


def test_pausing_turns_memory_off_for_every_chat_and_a_switch_for_one() -> None:
    paused = MemoryControls(is_paused=True, disabled_harnesses=())
    pi_off = MemoryControls(is_paused=False, disabled_harnesses=(MemoryHarness.PI_CODING,))

    assert not is_memory_on_for(paused, MemoryHarness.CLAUDE)
    assert not is_memory_on_for(paused, MemoryHarness.PI_CODING)
    assert is_memory_on_for(pi_off, MemoryHarness.CLAUDE)
    assert not is_memory_on_for(pi_off, MemoryHarness.PI_CODING)


def test_saving_round_trips_and_sets_claudes_switch_keeping_its_other_settings(tmp_path: Path) -> None:
    controls_path, claude_path = _paths(tmp_path)
    claude_path.parent.mkdir()
    claude_path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    paused = MemoryControls(is_paused=True, disabled_harnesses=(MemoryHarness.PI_CODING,))

    save_controls(paused, controls_path, claude_path)

    assert read_controls(controls_path) == paused
    assert json.loads(claude_path.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}, "autoMemoryEnabled": False}

    save_controls(MemoryControls(is_paused=False, disabled_harnesses=()), controls_path, claude_path)

    assert json.loads(claude_path.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}}


def test_turning_off_only_pi_leaves_claude_alone_and_creates_no_claude_settings(tmp_path: Path) -> None:
    controls_path, claude_path = _paths(tmp_path)

    save_controls(
        MemoryControls(is_paused=False, disabled_harnesses=(MemoryHarness.PI_CODING,)), controls_path, claude_path
    )

    assert json.loads(controls_path.read_text()) == {"is_paused": False, "disabled_harnesses": ["PI_CODING"]}
    assert not claude_path.exists()


@pytest.mark.parametrize("claude_settings", ["{not json", "[1, 2]"])
def test_a_claude_settings_file_that_is_not_a_json_object_refuses_the_save(
    tmp_path: Path, claude_settings: str
) -> None:
    controls_path, claude_path = _paths(tmp_path)
    claude_path.parent.mkdir()
    claude_path.write_text(claude_settings)

    with pytest.raises(ControlsReadError, match="Claude's memory switch was not changed"):
        save_controls(MemoryControls(is_paused=True, disabled_harnesses=()), controls_path, claude_path)

    assert not controls_path.exists()
    assert claude_path.read_text() == claude_settings


# The same table is in system/scripts/agent_memory_context_test.py: the page and the chats must read a settings file
# the same way, or the page would say memory is off while chats use it.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"is_paused": false, "disabled_harnesses": [], "added_later": 1}', MemoryControls()),
        (
            '{"is_paused": false, "disabled_harnesses": ["CODEX", "PI_CODING", 3]}',
            MemoryControls(disabled_harnesses=(MemoryHarness.PI_CODING,)),
        ),
        ('{"is_paused": true}', MemoryControls(is_paused=True)),
        ("{}", MemoryControls()),
    ],
)
def test_settings_ignore_keys_and_harnesses_this_version_does_not_know(
    tmp_path: Path, text: str, expected: MemoryControls
) -> None:
    controls_path = tmp_path / "settings.json"
    controls_path.write_text(text)

    assert read_controls(controls_path) == expected


@pytest.mark.parametrize(
    "text", [b"{", b"[]", b'{"is_paused": "true"}', b'{"is_paused": 1}', b'{"disabled_harnesses": "CLAUDE"}', b"\xff"]
)
def test_settings_that_cannot_be_read_raise_rather_than_reading_as_on(tmp_path: Path, text: bytes) -> None:
    controls_path = tmp_path / "settings.json"
    controls_path.write_bytes(text)

    with pytest.raises(ControlsReadError):
        read_controls(controls_path)
