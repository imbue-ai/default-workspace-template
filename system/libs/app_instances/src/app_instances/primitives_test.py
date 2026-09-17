import pytest

from app_instances.errors import InvalidInstanceValueError
from app_instances.primitives import (
    MAX_INSTANCE_KEY_LENGTH,
    MAX_INSTANCE_KEY_PREFIX_LENGTH,
    MAX_INSTANCE_TITLE_LENGTH,
    MAX_INSTANCE_URL_LENGTH,
    AbsoluteHttpUrl,
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    InstanceUrl,
    LocationPath,
    TitleTemplate,
    canonical_name_from_title,
    is_name_conflict,
    render_title_template,
)


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "terminal-2",
        "A.b_c-9",
        "x" * MAX_INSTANCE_KEY_LENGTH,
        "agent.session",
        "9lives",
    ],
)
def test_instance_key_accepts_url_safe_keys(value: str) -> None:
    assert InstanceKey(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-lead",
        ".lead",
        "_lead",
        "has space",
        "x" * (MAX_INSTANCE_KEY_LENGTH + 1),
        "slash/x",
        "percent%20",
        "café",
        "trailing-newline\n",
    ],
)
def test_instance_key_rejects_keys_outside_the_rule(value: str) -> None:
    with pytest.raises(InvalidInstanceValueError, match="invalid instance key"):
        InstanceKey(value)


@pytest.mark.parametrize(
    "value", ["files", "terminal", "a.b_c", "x" * MAX_INSTANCE_KEY_PREFIX_LENGTH]
)
def test_instance_key_prefix_accepts_the_key_alphabet(value: str) -> None:
    assert InstanceKeyPrefix(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "-x", "with space", "files\n", "x" * (MAX_INSTANCE_KEY_PREFIX_LENGTH + 1)],
)
def test_instance_key_prefix_rejects_prefixes_that_cannot_head_a_key(
    value: str,
) -> None:
    with pytest.raises(InvalidInstanceValueError, match="invalid key prefix"):
        InstanceKeyPrefix(value)


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/docs/",
        "/?arg=session&arg=terminal-2&arg={tab}",
        "/a b",
        "/" + "x" * (MAX_INSTANCE_URL_LENGTH - 1),
    ],
)
def test_instance_url_accepts_rooted_paths_with_at_most_one_placeholder(
    value: str,
) -> None:
    assert InstanceUrl(value) == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "single slash"),
        ("relative", "single slash"),
        ("//evil.example", "single slash"),
        ("/" + "x" * MAX_INSTANCE_URL_LENGTH, "over the"),
        ("/{tab}/{tab}", "at most once"),
        ("/line\nbreak", "control characters"),
        ("/del\x7f", "control characters"),
    ],
)
def test_instance_url_rejects_unrooted_overlong_doubled_placeholder_or_control_characters(
    value: str, reason: str
) -> None:
    with pytest.raises(InvalidInstanceValueError, match=reason):
        InstanceUrl(value)


def test_location_path_accepts_a_rooted_path_and_rejects_the_placeholder() -> None:
    assert LocationPath("/data/docs/?x=1") == "/data/docs/?x=1"
    with pytest.raises(InvalidInstanceValueError, match="not allowed here"):
        LocationPath("/?arg={tab}")


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "http://127.0.0.1:8080/path?query=1#fragment",
        "https://example.com/"
        + "x" * (MAX_INSTANCE_URL_LENGTH - len("https://example.com/")),
    ],
)
def test_absolute_http_url_accepts_web_urls(value: str) -> None:
    assert AbsoluteHttpUrl(value) == value


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("/data/docs/", "expected an absolute http or https URL"),
        ("ftp://example.com/file", "expected an absolute http or https URL"),
        ("https:///no-host", "expected an absolute http or https URL"),
        ("example.com", "expected an absolute http or https URL"),
        ("https://example.com/a b", "whitespace and control characters"),
        ("https://example.com/\x07", "whitespace and control characters"),
        ("https://example.com/" + "x" * MAX_INSTANCE_URL_LENGTH, "over the"),
    ],
)
def test_absolute_http_url_rejects_paths_other_schemes_and_bad_characters(
    value: str, problem: str
) -> None:
    with pytest.raises(InvalidInstanceValueError, match=problem):
        AbsoluteHttpUrl(value)


def test_instance_title_is_trimmed_and_bounded() -> None:
    assert InstanceTitle("  Terminal 2 \n") == "Terminal 2"
    assert (
        InstanceTitle("x" * MAX_INSTANCE_TITLE_LENGTH)
        == "x" * MAX_INSTANCE_TITLE_LENGTH
    )
    with pytest.raises(InvalidInstanceValueError, match="must not be blank"):
        InstanceTitle("   ")
    with pytest.raises(InvalidInstanceValueError, match="over the"):
        InstanceTitle("x" * (MAX_INSTANCE_TITLE_LENGTH + 1))


def test_title_template_requires_the_number_placeholder() -> None:
    assert TitleTemplate("File Viewer {n}") == "File Viewer {n}"
    with pytest.raises(InvalidInstanceValueError, match="must contain"):
        TitleTemplate("File Viewer")


def test_render_title_template_fills_in_the_number_as_a_title() -> None:
    rendered = render_title_template(TitleTemplate("File Viewer {n}"), 12)

    assert rendered == "File Viewer 12"
    assert isinstance(rendered, InstanceTitle)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Build", "My-Build"),
        ("  spaced   out  ", "spaced-out"),
        ("terminal-3", "terminal-3"),
        ("Chat 2", "Chat-2"),
        ("--dashes--", "dashes"),
        ("emoji only ☃", "emoji-only"),
        ("☃", ""),
        ("dots.and:colons", "dotsandcolons"),
    ],
)
def test_canonical_name_from_title_mirrors_the_shells_true_name_rule(
    title: str, expected: str
) -> None:
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
