import pytest

from app_instances.errors import InvalidInstanceValueError
from app_instances.primitives import (
    MAX_INSTANCE_KEY_LENGTH,
    MAX_INSTANCE_KEY_PREFIX_LENGTH,
    MAX_INSTANCE_TITLE_LENGTH,
    MAX_INSTANCE_URL_LENGTH,
    InstanceKey,
    InstanceKeyPrefix,
    InstanceTitle,
    InstanceUrl,
    LocationPath,
    TitleTemplate,
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
    "value", ["", "-x", "with space", "x" * (MAX_INSTANCE_KEY_PREFIX_LENGTH + 1)]
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
