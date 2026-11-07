"""The display settings a Screen reads from its environment at service start (docs/system/specs/screen/spec.md
sections 4.3, 5.3, 7.2 and 8.1): the video codec and quality, a fixed display size, a pinned video profile, whether the
desktop session runs its panel, and whether neko runs ICE lite. An auth sandbox sets all six; a workspace sets none."""

from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure
from screen.data_types import DisplaySettings
from screen.data_types import ScreenSize
from screen.data_types import VideoCodec
from screen.data_types import VideoQuality
from screen.errors import InvalidScreenSizeError
from screen.errors import ScreenConfigurationError
from screen.primitives import MAX_HEIGHT
from screen.primitives import MAX_WIDTH
from screen.primitives import MIN_HEIGHT
from screen.primitives import MIN_WIDTH
from screen.primitives import VIDEO_IDS
from screen.primitives import parse_size_argument
from screen.xorg_config import CVT_CELL_GRANULARITY_PX
from screen.xorg_config import MODE_REFRESH_HZ

ENV_VIDEO_CODEC: Final[str] = "SCREEN_VIDEO_CODEC"
ENV_VIDEO_QUALITY: Final[str] = "SCREEN_VIDEO_QUALITY"
ENV_FIXED_SIZE: Final[str] = "SCREEN_FIXED_SIZE"
ENV_VIDEO_PROFILE: Final[str] = "SCREEN_VIDEO_PROFILE"
ENV_DESKTOP_PANEL: Final[str] = "SCREEN_DESKTOP_PANEL"
ENV_ICE_LITE: Final[str] = "SCREEN_ICE_LITE"
_SWITCH_VALUES: Final[dict[str, bool]] = {"on": True, "off": False}
# Both encoders take I420, which halves the chroma rows, so a fixed height is a multiple of this.
_FIXED_HEIGHT_MULTIPLE: Final[int] = 2


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
def parse_video_quality(value: str, codec: VideoCodec) -> VideoQuality:
    """``standard`` or ``high``, in any case; empty is the default, standard. ``high`` tunes the VP8 pipelines alone,
    so it needs ``SCREEN_VIDEO_CODEC=vp8``."""
    if not value:
        return VideoQuality.STANDARD
    if value.upper() not in VideoQuality.__members__:
        names = ", ".join(quality.value.lower() for quality in VideoQuality)
        raise ScreenConfigurationError(f"{ENV_VIDEO_QUALITY} names {value!r}, not one of {names}")
    quality = VideoQuality[value.upper()]
    if quality == VideoQuality.HIGH and codec != VideoCodec.VP8:
        raise ScreenConfigurationError(f"{ENV_VIDEO_QUALITY}=high needs {ENV_VIDEO_CODEC}=vp8")
    return quality


@pure
def parse_fixed_size(value: str) -> ScreenSize | None:
    """``WxH`` within 640x480 and 1920x1080, its width a whole number of CVT's 8-pixel cells (the modeline keeps no
    other width) and its height even; empty is no fixed size. Not only a size a viewer's report could produce, since
    no report is applied while the size is fixed: the auth sandbox's display is exactly its sign-in window."""
    if not value:
        return None
    try:
        width, height = parse_size_argument(value)
    except InvalidScreenSizeError as e:
        raise ScreenConfigurationError(f"{ENV_FIXED_SIZE}: {e}") from e
    if not (MIN_WIDTH <= width <= MAX_WIDTH and MIN_HEIGHT <= height <= MAX_HEIGHT):
        raise ScreenConfigurationError(
            f"{ENV_FIXED_SIZE} is {value!r}, outside {MIN_WIDTH}x{MIN_HEIGHT} to {MAX_WIDTH}x{MAX_HEIGHT}"
        )
    if width % CVT_CELL_GRANULARITY_PX != 0 or height % _FIXED_HEIGHT_MULTIPLE != 0:
        raise ScreenConfigurationError(
            f"{ENV_FIXED_SIZE} is {value!r}; the width must be a multiple of {CVT_CELL_GRANULARITY_PX} "
            "and the height even"
        )
    return ScreenSize(width=width, height=height, rate=MODE_REFRESH_HZ)


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
    video_codec = parse_video_codec(environ.get(ENV_VIDEO_CODEC, "").strip())
    return DisplaySettings(
        video_codec=video_codec,
        video_quality=parse_video_quality(environ.get(ENV_VIDEO_QUALITY, "").strip(), video_codec),
        fixed_size=parse_fixed_size(environ.get(ENV_FIXED_SIZE, "").strip()),
        pinned_video_id=parse_video_profile(environ.get(ENV_VIDEO_PROFILE, "").strip()),
        is_desktop_panel_shown=parse_switch(ENV_DESKTOP_PANEL, environ.get(ENV_DESKTOP_PANEL, "").strip(), True),
        is_ice_lite=parse_switch(ENV_ICE_LITE, environ.get(ENV_ICE_LITE, "").strip(), False),
    )
