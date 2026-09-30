from typing import Any

import pytest

from app_manifest.errors import PageTemplateFieldError
from app_manifest.primitives import PageTemplate
from app_manifest.primitives import canonical_name_from_title
from app_manifest.primitives import is_name_conflict
from app_manifest.primitives import render_page_template


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


@pytest.mark.parametrize(
    ("template", "fields", "expected"),
    [
        ("{path}?view", {"path": "/home/user/notes/plan.md"}, "/home/user/notes/plan.md?view"),
        ("{path}?view", {"path": "/home/user/my notes/a?b#c.txt"}, "/home/user/my%20notes/a%3Fb%23c.txt?view"),
        ("{path}", {"path": "/home/user/caf\u00e9/\u2603.md"}, "/home/user/caf%C3%A9/%E2%98%83.md"),
        ("{path}/?view", {"path": "/data/q4 (final)"}, "/data/q4%20(final)/?view"),
        ("/open/{name}", {"name": "a/b"}, "/open/a/b"),
    ],
)
def test_a_page_template_percent_encodes_each_field_one_path_segment_at_a_time(
    template: str, fields: dict[str, Any], expected: str
) -> None:
    assert render_page_template(PageTemplate(template), fields) == expected


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        pytest.param({}, "'path' field is missing", id="missing"),
        pytest.param({"path": 7}, "'path' field is missing or not a string", id="not-a-string"),
        pytest.param({"path": "notes/plan.md"}, "not a rooted path", id="relative"),
        pytest.param({"path": "//evil.example/plan.md"}, "not a rooted path", id="another-host"),
    ],
)
def test_a_message_that_cannot_fill_a_page_template_is_refused(fields: dict[str, Any], match: str) -> None:
    with pytest.raises(PageTemplateFieldError, match=match):
        render_page_template(PageTemplate("{path}?view"), fields)
