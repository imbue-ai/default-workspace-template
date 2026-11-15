from imbue.chat.harnesses.reply_draft_matching import is_landed_reply


def test_is_landed_reply_ignores_the_markdown_the_reverse_mapping_cannot_restore() -> None:
    landed = ["## Steps\n\n```python\nprint('hi')\n```\n\n1. First"]

    assert is_landed_reply("**Steps**\n\n```\nprint('hi')\n```", landed)
    assert not is_landed_reply("A different reply entirely.", landed)
    assert is_landed_reply("   ", landed)
