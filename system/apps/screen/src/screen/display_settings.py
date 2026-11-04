"""The display settings a Screen reads from its environment at service start (docs/system/specs/screen/spec.md
sections 7.2 and 8.1): the video codec, and a fixed display size. An auth sandbox sets both; a workspace sets
neither."""

from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure
from screen.data_types import DisplaySettings
from screen.data_types import ScreenSize
from screen.data_types import VideoCodec
from screen.errors import InvalidScreenSizeError
from screen.errors import ScreenConfigurationError
from screen.primitives import parse_size_argument
from screen.sizing import quantize_screen_size

ENV_VIDEO_CODEC: Final[str] = "SCREEN_VIDEO_CODEC"
ENV_FIXED_SIZE: Final[str] = "SCREEN_FIXED_SIZE"


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
def display_settings_from_environ(environ: Mapping[str, str]) -> DisplaySettings:
    return DisplaySettings(
        video_codec=parse_video_codec(environ.get(ENV_VIDEO_CODEC, "").strip()),
        fixed_size=parse_fixed_size(environ.get(ENV_FIXED_SIZE, "").strip()),
    )
