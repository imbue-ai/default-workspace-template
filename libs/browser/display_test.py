"""Unit tests for the keysym mapping in browser.display (no X server needed --
importing the module and mapping keys never opens a display)."""

from Xlib import XK

from browser import display


def test_code_table_covers_letters_digits_and_named_keys() -> None:
    assert display._CODE_TO_KEYSYM_NAME["KeyA"] == "a"
    assert display._CODE_TO_KEYSYM_NAME["KeyZ"] == "z"
    assert display._CODE_TO_KEYSYM_NAME["Digit1"] == "1"
    assert display._CODE_TO_KEYSYM_NAME["Enter"] == "Return"
    assert display._CODE_TO_KEYSYM_NAME["ControlLeft"] == "Control_L"
    assert display._CODE_TO_KEYSYM_NAME["F5"] == "F5"


def test_keysym_for_letter_uses_the_physical_code_not_the_shifted_char() -> None:
    # 'A' is typed as Shift + the physical 'a' key; XTest presses the base keycode
    # while Shift (its own event) is held. So KeyA must map to the base keysym 'a',
    # never the keysym 'A' -- otherwise the shift level would be applied twice.
    assert display._keysym_for("KeyA", "A") == XK.string_to_keysym("a")


def test_keysym_for_named_key() -> None:
    assert display._keysym_for("Enter", "Enter") == XK.string_to_keysym("Return")
    assert display._keysym_for("ArrowLeft", "ArrowLeft") == XK.string_to_keysym("Left")


def test_extra_keys_are_mapped() -> None:
    # Keys that were silently dropped before (their `key` names are multi-char so the
    # char fallback returns 0) now resolve via the code table.
    assert display._CODE_TO_KEYSYM_NAME["NumLock"] == "Num_Lock"
    assert display._CODE_TO_KEYSYM_NAME["ContextMenu"] == "Menu"
    assert display._CODE_TO_KEYSYM_NAME["F13"] == "F13"
    assert display._keysym_for("NumLock", "NumLock") == XK.string_to_keysym("Num_Lock")
    assert display._keysym_for("F13", "F13") == XK.string_to_keysym("F13")
    assert display._CODE_TO_KEYSYM_NAME["Numpad1"] == "KP_1"  # digit, not KP_End (NumLock on)


def test_keysym_for_unknown_code_falls_back_to_the_character() -> None:
    # A layout/key our table misses still types via the produced character.
    assert display._keysym_for("IntlBackslash", "é") == ord("é")  # Latin-1 == codepoint
    assert display._keysym_for("SomeKey", "☃") == 0x01000000 + ord("☃")  # Unicode plane


def test_keysym_for_returns_zero_when_unmappable() -> None:
    # No code-table entry and no single character -> nothing to inject.
    assert display._keysym_for("SuperUnknown", "") == 0
    assert display._keysym_for("", "Shift") == 0  # multi-char non-code name


def test_shifted_char_and_base_char_share_a_keysym() -> None:
    # The physical-code mapping means 'A' and 'a' resolve to the same keysym (the base
    # key); Shift (its own event) supplies the case. This is the core correctness prop.
    assert display._keysym_for("KeyA", "A") == display._keysym_for("KeyA", "a")
