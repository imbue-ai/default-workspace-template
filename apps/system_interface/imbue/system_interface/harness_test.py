import pytest

from imbue.system_interface.harness import Harness
from imbue.system_interface.harness import parse_harness


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude", Harness.CLAUDE),
        ("claude-main", Harness.CLAUDE),
        ("claude-worker", Harness.CLAUDE),
        ("codex", Harness.CODEX),
        ("codex-main", Harness.CODEX),
        ("codex-worker", Harness.CODEX),
        ("antigravity", Harness.ANTIGRAVITY),
        ("antigravity-main", Harness.ANTIGRAVITY),
        ("antigravity-worker", Harness.ANTIGRAVITY),
        ("opencode", Harness.OPENCODE),
        ("opencode-main", Harness.OPENCODE),
        ("opencode-worker", Harness.OPENCODE),
    ],
)
def test_parse_harness_strips_role_suffix(raw: str, expected: Harness) -> None:
    assert parse_harness(raw) == expected


def test_parse_harness_returns_none_for_unrecognized_type() -> None:
    assert parse_harness("some-future-harness") is None
    assert parse_harness("mngr-proxy-child") is None
    assert parse_harness("") is None
