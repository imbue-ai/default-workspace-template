from imbue.chat.dictation_note import without_dictation_notes

_NOTE = "<dictation-note>\nSome of this message was dictated, so read it leniently.\n</dictation-note>"


def test_a_message_loses_its_note_and_the_blank_line_before_it() -> None:
    assert without_dictation_notes(f"Rename the parser module\n\n{_NOTE}") == "Rename the parser module"


def test_a_message_without_a_note_is_returned_as_it_is() -> None:
    message = "Rename the parser module\n\nand its <tests>"

    assert without_dictation_notes(message) == message


def test_a_note_is_recognised_by_its_tags_whatever_it_says() -> None:
    reworded = "<dictation-note>\nAnother wording.\nOver two lines.\n</dictation-note>"

    assert without_dictation_notes(f"Ship it\n\n{reworded}") == "Ship it"


def test_every_note_goes_from_a_text_that_joins_several_messages() -> None:
    joined = f"first message\n\n{_NOTE}\nsecond message, typed\nthird message\n\n{_NOTE}"

    assert without_dictation_notes(joined) == "first message\nsecond message, typed\nthird message"


def test_the_attachment_block_after_a_note_keeps_its_blank_line() -> None:
    attachment = "See attachment here: ![/code/uploads/aaa/diagram.png](/code/uploads/aaa/diagram.png)"

    assert without_dictation_notes(f"look at this\n\n{_NOTE}\n\n{attachment}") == f"look at this\n\n{attachment}"


def test_a_note_alone_leaves_nothing() -> None:
    assert without_dictation_notes(_NOTE) == ""
