import pytest

from workspace_themes.errors import InvalidThemeValueError
from workspace_themes.primitives import (
    ThemeAuthor,
    ThemeDescription,
    ThemeName,
    ThemeRelativePath,
)


@pytest.mark.parametrize("text_type", [ThemeName, ThemeDescription, ThemeAuthor])
@pytest.mark.parametrize("value", ["A\x7fB", "A\x1b[31mB", "A\nB", "A\tB"])
def test_theme_text_refuses_control_characters(
    text_type: type[ThemeName], value: str
) -> None:
    with pytest.raises(InvalidThemeValueError, match="control characters"):
        text_type(value)


def test_theme_text_is_trimmed() -> None:
    assert ThemeName("  Sepia \U0001d11e  ") == "Sepia \U0001d11e"


@pytest.mark.parametrize(
    "value",
    ["./x.css", "a/./b.css", "a//b.css", "x.css/", "../x.css", "a/../b.css", "/x.css"],
)
def test_a_theme_path_refuses_any_segment_that_is_not_a_name(value: str) -> None:
    with pytest.raises(InvalidThemeValueError):
        ThemeRelativePath(value)


def test_a_theme_path_takes_a_plain_relative_path() -> None:
    assert ThemeRelativePath("vendor/assets/button.svg") == "vendor/assets/button.svg"
