import pytest

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import WallpaperName
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowTitle


def test_desktop_ids_are_slugs() -> None:
    assert DesktopId("research-2") == "research-2"
    with pytest.raises(InvalidLayoutValueError):
        DesktopId("Not A Slug")


def test_window_ids_are_the_shells_minted_shape() -> None:
    assert WindowId("win-0123456789abcdef") == "win-0123456789abcdef"
    for malformed in ("tab-0123456789abcdef", "win-0123", "win-0123456789ABCDEF"):
        with pytest.raises(InvalidLayoutValueError):
            WindowId(malformed)


def test_client_ids_and_wallpaper_names_are_filename_safe() -> None:
    assert ClientId("3f2a-desktop.1") == "3f2a-desktop.1"
    assert WallpaperName("apricot-coast") == "apricot-coast"
    for malformed in ("../escape", "", "a b"):
        with pytest.raises(InvalidLayoutValueError):
            ClientId(malformed)
        with pytest.raises(InvalidLayoutValueError):
            WallpaperName(malformed)


def test_window_paths_are_rooted_on_the_apps_origin_and_titles_are_trimmed() -> None:
    assert WindowPath("/?chat=agent-1") == "/?chat=agent-1"
    for bad in ("", "notes", "//evil.example", "/\x00"):
        with pytest.raises(InvalidLayoutValueError):
            WindowPath(bad)
    assert WindowTitle("  Build log  ") == "Build log"
    assert WindowTitle("") == ""
    with pytest.raises(InvalidLayoutValueError):
        WindowTitle("x" * 300)


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "workspace-agent",
        "user:user-bob-4471",
        "email:bob.smith+tag@example.com",
        "agent:va-0123456789abcdef0123456789abcdef",
        "name:Ana María",
        "name:O'Brien (QA)",
    ],
)
def test_participant_keys_take_each_form(value: str) -> None:
    assert ParticipantKey(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "email:a/b@example.com",
        "email:a@example.com/x",
        "email:a#b@example.com",
        "email:a?b@example.com",
        "email:a%40b@example.com",
        "email:a@exa%mple.com",
        "email:a:b@example.com",
        "email:nobody",
        "name:../escape",
        "name:a/b",
        "name:tab\there",
        "name:",
        "name:" + "x" * 61,
        "user:bob smith",
        "guest",
    ],
)
def test_participant_keys_refuse_what_a_file_name_or_a_path_segment_cannot_carry(value: str) -> None:
    with pytest.raises(InvalidLayoutValueError):
        ParticipantKey(value)
