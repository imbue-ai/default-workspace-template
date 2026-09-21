import pytest

from app_manifest.primitives import canonical_name_from_title
from app_manifest.primitives import is_name_conflict


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Build", "My-Build"),
        ("  spaced   out  ", "spaced-out"),
        ("terminal-3", "terminal-3"),
        ("Chat 2", "Chat-2"),
        ("--dashes--", "dashes"),
        ("emoji only \u2603", "emoji-only"),
        ("\u2603", ""),
        ("dots.and:colons", "dotsandcolons"),
    ],
)
def test_canonical_name_from_title_mirrors_the_true_name_rule(title: str, expected: str) -> None:
    assert canonical_name_from_title(title) == expected


@pytest.mark.parametrize(
    ("candidate", "taken", "expected"),
    [
        ("My Build", ["My-Build"], True),
        ("my build", ["My-Build"], True),
        ("Build", ["build-2"], False),
        ("terminal 2", ["terminal-1", "terminal-2"], True),
        ("Fresh", [], False),
    ],
)
def test_is_name_conflict_compares_canonical_forms_case_insensitively(
    candidate: str, taken: list[str], expected: bool
) -> None:
    assert is_name_conflict(candidate, taken) is expected
