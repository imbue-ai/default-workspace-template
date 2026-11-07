"""The display settings a Screen reads from its environment at service start (docs/system/specs/screen/spec.md
sections 4.3, 5.3, 7.2 and 8.1): the video codec, a fixed display size, a pinned video profile, whether the desktop
session runs its panel, and whether neko runs ICE lite. An auth sandbox sets all five; a workspace sets none."""

from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure
from screen.data_types import DisplaySettings
from screen.data_types import ScreenSize
from screen.data_types import VideoCodec
from screen.errors import InvalidScreenSizeError
from screen.errors import ScreenConfigurationError
from screen.primitives import VIDEO_IDS
from screen.primitives import parse_size_argument
from screen.sizing import quantize_screen_size

ENV_VIDEO_CODEC: Final[str] = "SCREEN_VIDEO_CODEC"
ENV_FIXED_SIZE: Final[str] = "SCREEN_FIXED_SIZE"
ENV_VIDEO_PROFILE: Final[str] = "SCREEN_VIDEO_PROFILE"
ENV_DESKTOP_PANEL: Final[str] = "SCREEN_DESKTOP_PANEL"
ENV_ICE_LITE: Final[str] = "SCREEN_ICE_LITE"
_SWITCH_VALUES: Final[dict[str, bool]] = {"on": True, "off": False}


@pure
def parse_video_codec(value: str) -> VideoCodec:
    """``h264`` or ``vp8``, in any case; empty is the default, H.264."""
    if not value:
        return VideoCodec.H264
    if value.upper() not in VideoCodec.__members__:
        names = ", ".join(codec.value.lower() for codec in VideoCodec)
        raise ScreenConfigurationError(f"{ENV_VIDEO_CODEC} names {value!r}, not one of {names}")
    return VideoCodec[value.upper()]


@pure
def parse_fixed_size(value: str) -> ScreenSize | None:
    """``WxH``, which must already be a size a viewer's report could produce (multiples of 16, or the cap, within
    640x480 and 1920x1080); empty is no fixed size."""
    if not value:
        return None
    try:
        width, height = parse_size_argument(value)
    except InvalidScreenSizeError as e:
        raise ScreenConfigurationError(f"{ENV_FIXED_SIZE}: {e}") from e
    size = quantize_screen_size(width, height)
    if (size.width, size.height) != (width, height):
        raise ScreenConfigurationError(
            f"{ENV_FIXED_SIZE} is {value!r}, which the Screen would take as {size.width}x{size.height}; "
            "set that instead"
        )
    return size


@pure
def parse_video_profile(value: str) -> str | None:
    """A profile's id (``idle``, ``watch`` or ``control``), in any case; empty is no pinned profile."""
    if not value:
        return None
    if value.lower() not in VIDEO_IDS:
        raise ScreenConfigurationError(f"{ENV_VIDEO_PROFILE} names {value!r}, not one of {', '.join(VIDEO_IDS)}")
    return value.lower()


@pure
def parse_switch(name: str, value: str, default: bool) -> bool:
    """``on`` or ``off``, in any case; empty is ``default``."""
    if not value:
        return default
    if value.lower() not in _SWITCH_VALUES:
        raise ScreenConfigurationError(f"{name} names {value!r}, not on or off")
    return _SWITCH_VALUES[value.lower()]


@pure
def display_settings_from_environ(environ: Mapping[str, str]) -> DisplaySettings:
    return DisplaySettings(
        video_codec=parse_video_codec(environ.get(ENV_VIDEO_CODEC, "").strip()),
        fixed_size=parse_fixed_size(environ.get(ENV_FIXED_SIZE, "").strip()),
        pinned_video_id=parse_video_profile(environ.get(ENV_VIDEO_PROFILE, "").strip()),
        is_desktop_panel_shown=parse_switch(ENV_DESKTOP_PANEL, environ.get(ENV_DESKTOP_PANEL, "").strip(), True),
        is_ice_lite=parse_switch(ENV_ICE_LITE, environ.get(ENV_ICE_LITE, "").strip(), False),
    )
