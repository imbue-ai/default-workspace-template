"""The ``neko.yaml`` and the PulseAudio script the Screen's stack runs with (docs/system/specs/screen/spec.md
sections 5.3, 6.2, 6.3, 8.1 and 9.1), rendered at each stack start with that start's secrets."""

from typing import Any
from typing import Final

import yaml
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.data_types import DisplaySettings
from screen.data_types import NekoSecrets
from screen.data_types import VideoCodec
from screen.data_types import VideoQuality
from screen.primitives import DISPLAY
from screen.primitives import ICE_MUX_PORT
from screen.primitives import MAX_HEIGHT
from screen.primitives import MAX_WIDTH
from screen.primitives import NEKO_HOST
from screen.primitives import NEKO_PORT
from screen.primitives import RUNTIME_DIR
from screen.primitives import VIDEO_IDS
from screen.primitives import VIDEO_ID_CONTROL
from screen.primitives import VIDEO_ID_IDLE
from screen.primitives import VIDEO_ID_WATCH
from screen.xorg_config import INPUT_SOCKET_PATH
from screen.xorg_config import MODE_REFRESH_HZ

# The PulseAudio sink neko captures, and where the other members reach the daemon.
AUDIO_SINK_NAME: Final[str] = "audio_output"
AUDIO_CAPTURE_DEVICE: Final[str] = f"{AUDIO_SINK_NAME}.monitor"
PULSE_SOCKET_PATH: Final[str] = f"{RUNTIME_DIR}/pulse.sock"

# The JPEG screencast every client falls back to without WebRTC (spec section 5.4): a low rate, since each frame is a
# full JPEG of the display and the fallback is for seeing the Screen, not for smooth motion.
SCREENCAST_RATE: Final[str] = "5/1"
SCREENCAST_QUALITY: Final[str] = "60"


class VideoProfile(FrozenModel):
    """One of the three pipelines (spec section 8.1), each with a one-second keyframe interval; the rate and threads
    are the codec's own, the speed setting is per codec."""

    video_id: str = Field(description="The id viewers select the profile by")
    fps: int = Field(description="Frames per second")
    bitrate_kbps: int = Field(description="The encoder's target bitrate")
    threads: int = Field(description="The encoder's thread count")
    x264_speed_preset: str = Field(description="x264's speed preset")
    vp8_cpu_used: int = Field(description="libvpx's realtime speed, 0 to 16, higher faster and coarser")


VIDEO_PROFILES: Final[tuple[VideoProfile, ...]] = (
    VideoProfile(
        video_id=VIDEO_ID_IDLE, fps=5, bitrate_kbps=800, threads=1, x264_speed_preset="ultrafast", vp8_cpu_used=8
    ),
    VideoProfile(
        video_id=VIDEO_ID_WATCH, fps=20, bitrate_kbps=2500, threads=4, x264_speed_preset="veryfast", vp8_cpu_used=8
    ),
    VideoProfile(
        video_id=VIDEO_ID_CONTROL, fps=30, bitrate_kbps=4000, threads=4, x264_speed_preset="veryfast", vp8_cpu_used=8
    ),
)


class Vp8RateControl(FrozenModel):
    """libvpx's rate control at one video quality: the bitrate against the profile table's, the rate-control buffer in
    milliseconds of that bitrate, and the quantizer range (0 to 63)."""

    bitrate_multiplier: int = Field(description="The target bitrate as a multiple of the profile's")
    buffer_size_ms: int = Field(description="The rate-control buffer")
    buffer_initial_size_ms: int = Field(description="The buffer's fill at the start")
    buffer_optimal_size_ms: int = Field(description="The fill the rate control aims for")
    min_quantizer: int = Field(description="The finest quantizer, which a still picture is refined down to")
    max_quantizer: int = Field(description="The coarsest quantizer")


VP8_RATE_CONTROL: Final[dict[VideoQuality, Vp8RateControl]] = {
    # WebRTC's own VP8 buffer, so a burst (a page scrolling) is spread over a second rather than sent at once; a cap
    # below libvpx's own quantizer keeps small text legible while the picture moves.
    VideoQuality.STANDARD: Vp8RateControl(
        bitrate_multiplier=1,
        buffer_size_ms=1000,
        buffer_initial_size_ms=500,
        buffer_optimal_size_ms=600,
        min_quantizer=4,
        max_quantizer=48,
    ),
    # For a sign-in's text: the doubled bitrate keeps scrolling text as sharp as still text, and the deeper buffer
    # lets the picture after a change, or after each keyframe, start sharp rather than refine over two seconds. The
    # keyframe interval stays one second: neko sends no keyframe on a viewer's request, so the interval bounds how long
    # a connecting viewer waits for its first picture.
    VideoQuality.HIGH: Vp8RateControl(
        bitrate_multiplier=2,
        buffer_size_ms=2000,
        buffer_initial_size_ms=1000,
        buffer_optimal_size_ms=1500,
        min_quantizer=2,
        max_quantizer=48,
    ),
}


@pure
def _x264_pipeline(profile: VideoProfile) -> dict[str, Any]:
    return {
        "gst_encoder": "x264enc",
        "gst_params": {
            "threads": str(profile.threads),
            "bitrate": str(profile.bitrate_kbps),
            "key-int-max": str(profile.fps),
            "byte-stream": "true",
            "tune": "zerolatency",
            "speed-preset": profile.x264_speed_preset,
        },
        "gst_suffix": "! video/x-h264,stream-format=byte-stream,profile=constrained-baseline",
    }


@pure
def _vp8_pipeline(profile: VideoProfile, quality: VideoQuality) -> dict[str, Any]:
    rate_control = VP8_RATE_CONTROL[quality]
    return {
        "gst_encoder": "vp8enc",
        "gst_params": {
            "deadline": "1",
            "cpu-used": str(profile.vp8_cpu_used),
            "threads": str(profile.threads),
            "lag-in-frames": "0",
            "end-usage": "cbr",
            # vp8enc counts bits per second; round() makes the product an integer (see _pipeline_config).
            "target-bitrate": f"round({profile.bitrate_kbps * rate_control.bitrate_multiplier} * 1000)",
            "undershoot": "95",
            "buffer-size": str(rate_control.buffer_size_ms),
            "buffer-initial-size": str(rate_control.buffer_initial_size_ms),
            "buffer-optimal-size": str(rate_control.buffer_optimal_size_ms),
            "min-quantizer": str(rate_control.min_quantizer),
            "max-quantizer": str(rate_control.max_quantizer),
            "keyframe-max-dist": str(profile.fps),
        },
        "gst_suffix": "",
    }


@pure
def _pipeline_config(profile: VideoProfile, settings: DisplaySettings) -> dict[str, Any]:
    # neko evaluates each gst_params value as a gval expression and writes ``key=<result>`` with Go's ``%v``: a
    # number is a float64, which ``%v`` prints in exponent form from a million up (``3e+06``, which an encoder
    # refuses), so a value that large is written ``round(...)``, which yields an integer; a bare word that names no
    # variable evaluates to nothing and is passed through as written, so a preset name works.
    encoder = (
        _x264_pipeline(profile)
        if settings.video_codec == VideoCodec.H264
        else _vp8_pipeline(profile, settings.video_quality)
    )
    return {
        "fps": str(profile.fps),
        "gst_prefix": "! video/x-raw,format=I420",
        **encoder,
        "show_pointer": False,
    }


@pure
def neko_config_document(secrets: NekoSecrets, settings: DisplaySettings) -> dict[str, Any]:
    """neko's configuration as a document: loopback HTTP, the two ICE muxes on the published port, the three
    profiles in the settings' codec and quality, the display at the fixed size or the cap, the multiuser provider with this
    start's passwords, the session rules of spec section 6.2."""
    width, height = (
        (settings.fixed_size.width, settings.fixed_size.height)
        if settings.fixed_size is not None
        else (MAX_WIDTH, MAX_HEIGHT)
    )
    return {
        "desktop": {
            "display": DISPLAY,
            "screen": f"{width}x{height}@{MODE_REFRESH_HZ}",
            # neko's xf86 input driver carries a phone's touches (spec section 14); neko dials its socket once at
            # start and exits when it is not there, so the stack starts neko only after Xorg has bound it.
            "input": {"enabled": True, "socket": INPUT_SOCKET_PATH},
            "unminimize": False,
            "upload_drop": True,
            "file_chooser_dialog": False,
        },
        "capture": {
            "video": {
                "display": DISPLAY,
                "codec": settings.video_codec.value.lower(),
                "ids": list(VIDEO_IDS),
                "pipelines": {profile.video_id: _pipeline_config(profile, settings) for profile in VIDEO_PROFILES},
                # The screencast pipeline takes this global flag (the per-pipeline one covers the WebRTC pipelines
                # alone), and the viewer draws its own pointer, so no cursor is painted into any picture.
                "show_pointer": False,
            },
            "audio": {"device": AUDIO_CAPTURE_DEVICE, "codec": "opus"},
            "screencast": {"enabled": True, "rate": SCREENCAST_RATE, "quality": SCREENCAST_QUALITY},
            "microphone": {"enabled": False},
            "webcam": {"enabled": False},
        },
        "server": {
            "bind": f"{NEKO_HOST}:{NEKO_PORT}",
            "metrics": False,
            "pprof": False,
        },
        "member": {
            "provider": "multiuser",
            "multiuser": {
                "user_password": secrets.user_password.get_secret_value(),
                "admin_password": secrets.admin_password.get_secret_value(),
            },
        },
        "session": {
            "api_token": secrets.api_token.get_secret_value(),
            "implicit_hosting": False,
            "control_protection": False,
            "private_mode": False,
            "inactive_cursors": True,
            "merciful_reconnect": True,
            "locked_controls": False,
            # The viewer carries its token itself, so neko answers the login with it instead of setting a cookie.
            "cookie": {"enabled": False, "secure": False},
        },
        "webrtc": {
            "udpmux": ICE_MUX_PORT,
            "tcpmux": ICE_MUX_PORT,
            "icetrickle": True,
            # The viewer rewrites candidates to the endpoints it is told (spec section 5.3), so neko never asks an
            # outside service what its address is.
            "ip_retrieval_url": "",
            # ICE lite (an auth sandbox's): neko answers the viewer's connectivity checks and starts none of its own,
            # which the one candidate it is reached on (a TCP tunnel) needs no more of.
            "icelite": settings.is_ice_lite,
            "estimator": {"enabled": False},
        },
    }


@pure
def render_neko_yaml(secrets: NekoSecrets, settings: DisplaySettings) -> str:
    return yaml.safe_dump(neko_config_document(secrets, settings), sort_keys=False, default_flow_style=False)


@pure
def render_pulse_script() -> str:
    """PulseAudio's load script: the null sink neko captures, the socket the members reach the daemon by."""
    return (
        f"load-module module-null-sink sink_name={AUDIO_SINK_NAME} "
        'sink_properties=device.description="Screen_Output"\n'
        f"load-module module-native-protocol-unix socket={PULSE_SOCKET_PATH} auth-anonymous=1\n"
        "load-module module-always-sink\n"
        f"set-default-sink {AUDIO_SINK_NAME}\n"
    )
