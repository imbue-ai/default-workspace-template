import base64

from imbue.chat.harnesses.claude.progress_updates import is_progress_update_signature
from imbue.chat.harnesses.claude.progress_updates import progress_update_text
from imbue.chat.testing import thinking_block_signature


def test_a_narration_signature_is_a_progress_update() -> None:
    assert is_progress_update_signature(thinking_block_signature("narration"))


def test_a_reasoning_signature_is_not_a_progress_update() -> None:
    assert not is_progress_update_signature(thinking_block_signature("thinking"))


def test_a_signature_without_its_padding_still_decodes() -> None:
    signature = thinking_block_signature("narration").rstrip("=")
    assert is_progress_update_signature(signature)


def test_the_word_narration_outside_the_kind_field_is_not_a_progress_update() -> None:
    assert not is_progress_update_signature(base64.b64encode(b"\x0a\x09narration").decode())


def test_undecodable_signatures_are_not_progress_updates() -> None:
    assert not is_progress_update_signature("not base64 !!")
    assert not is_progress_update_signature(base64.b64encode(b"\xff\xff\xff").decode())
    assert not is_progress_update_signature("")


def test_progress_update_text_is_the_trimmed_text_of_a_narration_block() -> None:
    block = {"type": "thinking", "thinking": "The data is in; matching by topic next.\n\n", "signature": thinking_block_signature("narration")}
    assert progress_update_text(block) == "The data is in; matching by topic next."


def test_progress_update_text_is_empty_for_reasoning_empty_and_other_blocks() -> None:
    assert progress_update_text({"type": "thinking", "thinking": "I'm weighing two options.", "signature": thinking_block_signature("thinking")}) == ""
    assert progress_update_text({"type": "thinking", "thinking": "", "signature": thinking_block_signature("narration")}) == ""
    assert progress_update_text({"type": "thinking", "thinking": "No signature here."}) == ""
    assert progress_update_text({"type": "text", "text": "Hello", "signature": thinking_block_signature("narration")}) == ""
