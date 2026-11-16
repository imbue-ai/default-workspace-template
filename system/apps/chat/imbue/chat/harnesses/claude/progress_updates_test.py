import base64

import pytest

from imbue.chat.harnesses.claude.progress_updates import progress_update_text
from imbue.chat.harnesses.claude.progress_updates import thinking_block_kind
from imbue.chat.testing import anthropic_thinking_block_signature


@pytest.mark.parametrize(
    ("signature", "kind"),
    [
        (anthropic_thinking_block_signature("narration"), b"narration"),
        (anthropic_thinking_block_signature("thinking"), b"thinking"),
        (anthropic_thinking_block_signature("narration").rstrip("="), b"narration"),
        (base64.b64encode(b"\x0a\x09narration").decode(), None),
        ("not base64 !!", None),
        (base64.b64encode(b"\xff\xff\xff").decode(), None),
        ("", None),
    ],
)
def test_thinking_block_kind_reads_the_kind_field_or_nothing(signature: str, kind: bytes | None) -> None:
    assert thinking_block_kind(signature) == kind


def _thinking(text: str, signature: str | None) -> dict[str, object]:
    block: dict[str, object] = {"type": "thinking", "thinking": text}
    if signature is not None:
        block["signature"] = signature
    return block


def test_a_narration_block_shows_its_trimmed_text() -> None:
    block = _thinking("The data is in; matching by topic next.\n\n", anthropic_thinking_block_signature("narration"))
    assert progress_update_text(block) == "The data is in; matching by topic next."


def test_a_reasoning_block_is_hidden_even_with_text() -> None:
    assert (
        progress_update_text(_thinking("I'm weighing two options.", anthropic_thinking_block_signature("thinking")))
        == ""
    )


@pytest.mark.parametrize("signature", [None, "not base64 !!", base64.b64encode(b"\x0a\x09narration").decode()])
def test_a_block_whose_kind_cannot_be_read_shows_its_text(signature: str | None) -> None:
    assert progress_update_text(_thinking("Checking the skills next.", signature)) == "Checking the skills next."


def test_empty_blocks_and_other_block_types_show_nothing() -> None:
    assert progress_update_text(_thinking("", anthropic_thinking_block_signature("narration"))) == ""
    assert (
        progress_update_text(
            {"type": "text", "text": "Hello", "signature": anthropic_thinking_block_signature("narration")}
        )
        == ""
    )
