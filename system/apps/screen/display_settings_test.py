import pytest

from screen.data_types import DisplaySettings
from screen.data_types import ScreenSize
from screen.data_types import VideoCodec
from screen.display_settings import display_settings_from_environ
from screen.display_settings import parse_fixed_size
from screen.display_settings import parse_switch
from screen.display_settings import parse_video_codec
from screen.errors import ScreenConfigurationError


def test_an_environment_without_the_settings_is_a_workspaces_screen() -> None:
    assert display_settings_from_environ({}) == DisplaySettings(
        video_codec=VideoCodec.H264, fixed_size=None, is_desktop_panel_shown=True, is_ice_lite=False
    )
    assert display_settings_from_environ({"SCREEN_VIDEO_CODEC": " ", "SCREEN_FIXED_SIZE": ""}) == DisplaySettings()


def test_an_auth_sandboxs_environment_fixes_the_size_streams_vp8_drops_the_panel_and_runs_ice_lite() -> None:
    settings = display_settings_from_environ(
        {
            "SCREEN_VIDEO_CODEC": "vp8",
            "SCREEN_FIXED_SIZE": "1360x864",
            "SCREEN_DESKTOP_PANEL": "off",
            "SCREEN_ICE_LITE": "on",
        }
    )
    assert settings == DisplaySettings(
        video_codec=VideoCodec.VP8,
        fixed_size=ScreenSize(width=1360, height=864, rate=60),
        is_desktop_panel_shown=False,
        is_ice_lite=True,
    )


def test_the_panel_and_ice_lite_switches_read_on_or_off_in_any_case_and_refuse_anything_else() -> None:
    assert display_settings_from_environ({"SCREEN_DESKTOP_PANEL": "ON", "SCREEN_ICE_LITE": "Off"}) == DisplaySettings(
        is_desktop_panel_shown=True, is_ice_lite=False
    )
    assert parse_switch("SCREEN_ICE_LITE", "", False) is False
    assert parse_switch("SCREEN_DESKTOP_PANEL", "", True) is True
    for name in ("SCREEN_DESKTOP_PANEL", "SCREEN_ICE_LITE"):
        with pytest.raises(ScreenConfigurationError, match=f"{name} names 'no', not on or off"):
            display_settings_from_environ({name: "no"})


def test_the_codec_is_read_in_any_case_and_an_unknown_one_is_refused() -> None:
    assert parse_video_codec("H264") == VideoCodec.H264
    assert parse_video_codec("Vp8") == VideoCodec.VP8
    with pytest.raises(ScreenConfigurationError, match="h264, vp8"):
        parse_video_codec("av1")


def test_a_fixed_size_must_be_one_a_viewers_report_could_produce() -> None:
    assert parse_fixed_size("1920x1080") == ScreenSize(width=1920, height=1080, rate=60)
    assert parse_fixed_size("640X480") == ScreenSize(width=640, height=480, rate=60)
    for unusable, taken_as in (("1366x768", "1360x768"), ("320x200", "640x480"), ("2560x1440", "1920x1080")):
        with pytest.raises(ScreenConfigurationError, match=taken_as):
            parse_fixed_size(unusable)
    for malformed in ("1360", "big", "1360x864x60"):
        with pytest.raises(ScreenConfigurationError, match="WIDTHxHEIGHT"):
            parse_fixed_size(malformed)
