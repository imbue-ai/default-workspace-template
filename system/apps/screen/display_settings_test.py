import pytest

from screen.data_types import DisplaySettings
from screen.data_types import ScreenSize
from screen.data_types import VideoCodec
from screen.data_types import VideoQuality
from screen.display_settings import display_settings_from_environ
from screen.display_settings import parse_fixed_size
from screen.display_settings import parse_switch
from screen.display_settings import parse_video_codec
from screen.display_settings import parse_video_profile
from screen.display_settings import parse_video_quality
from screen.errors import ScreenConfigurationError


def test_an_environment_without_the_settings_is_a_workspaces_screen() -> None:
    assert display_settings_from_environ({}) == DisplaySettings(
        video_codec=VideoCodec.H264,
        video_quality=VideoQuality.STANDARD,
        fixed_size=None,
        is_desktop_panel_shown=True,
        is_ice_lite=False,
        pinned_video_id=None,
    )
    assert display_settings_from_environ({"SCREEN_VIDEO_CODEC": " ", "SCREEN_FIXED_SIZE": ""}) == DisplaySettings()


def test_an_auth_sandboxs_environment_is_a_fixed_size_high_quality_vp8_control_stream_without_panel_on_ice_lite() -> (
    None
):
    settings = display_settings_from_environ(
        {
            "SCREEN_VIDEO_CODEC": "vp8",
            "SCREEN_VIDEO_QUALITY": "high",
            "SCREEN_FIXED_SIZE": "1288x806",
            "SCREEN_VIDEO_PROFILE": "control",
            "SCREEN_DESKTOP_PANEL": "off",
            "SCREEN_ICE_LITE": "on",
        }
    )
    assert settings == DisplaySettings(
        video_codec=VideoCodec.VP8,
        video_quality=VideoQuality.HIGH,
        fixed_size=ScreenSize(width=1288, height=806, rate=60),
        is_desktop_panel_shown=False,
        is_ice_lite=True,
        pinned_video_id="control",
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


def test_the_video_quality_is_read_in_any_case_and_high_needs_vp8() -> None:
    assert parse_video_quality("", VideoCodec.H264) == VideoQuality.STANDARD
    assert parse_video_quality("Standard", VideoCodec.H264) == VideoQuality.STANDARD
    assert parse_video_quality("HIGH", VideoCodec.VP8) == VideoQuality.HIGH
    settings = display_settings_from_environ({"SCREEN_VIDEO_CODEC": "vp8", "SCREEN_VIDEO_QUALITY": " high "})
    assert settings.video_quality == VideoQuality.HIGH
    with pytest.raises(ScreenConfigurationError, match="SCREEN_VIDEO_QUALITY names 'best', not one of standard, high"):
        display_settings_from_environ({"SCREEN_VIDEO_CODEC": "vp8", "SCREEN_VIDEO_QUALITY": "best"})
    with pytest.raises(ScreenConfigurationError, match="SCREEN_VIDEO_QUALITY=high needs SCREEN_VIDEO_CODEC=vp8"):
        display_settings_from_environ({"SCREEN_VIDEO_QUALITY": "high"})


def test_the_video_profile_is_a_profiles_id_in_any_case_and_an_unknown_one_is_refused() -> None:
    assert parse_video_profile("") is None
    assert parse_video_profile("CONTROL") == "control"
    assert parse_video_profile("idle") == "idle"
    assert display_settings_from_environ({"SCREEN_VIDEO_PROFILE": " Watch "}).pinned_video_id == "watch"
    with pytest.raises(
        ScreenConfigurationError, match="SCREEN_VIDEO_PROFILE names 'fast', not one of idle, watch, control"
    ):
        display_settings_from_environ({"SCREEN_VIDEO_PROFILE": "fast"})


def test_a_fixed_size_is_any_even_size_within_the_floor_and_the_cap_whose_width_is_whole_cvt_cells() -> None:
    assert parse_fixed_size("1920x1080") == ScreenSize(width=1920, height=1080, rate=60)
    assert parse_fixed_size("640X480") == ScreenSize(width=640, height=480, rate=60)
    # Not a size a viewer's report could produce (multiples of 16): the auth sandbox's sign-in window.
    assert parse_fixed_size("1288x806") == ScreenSize(width=1288, height=806, rate=60)
    for outside in ("632x480", "640x478", "1928x1080", "1920x1082"):
        with pytest.raises(ScreenConfigurationError, match="outside 640x480 to 1920x1080"):
            parse_fixed_size(outside)
    for unusable in ("1366x768", "1288x805"):
        with pytest.raises(ScreenConfigurationError, match="multiple of 8 and the height even"):
            parse_fixed_size(unusable)
    for malformed in ("1360", "big", "1360x864x60"):
        with pytest.raises(ScreenConfigurationError, match="WIDTHxHEIGHT"):
            parse_fixed_size(malformed)
