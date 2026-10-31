"""The ``neko.yaml`` and the PulseAudio script the Screen's stack runs with (docs/system/specs/screen/spec.md
sections 6.2, 6.3, 8.1 and 9.1), rendered at each stack start with that start's secrets."""

from typing import Any
from typing import Final

import yaml
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.data_types import NekoSecrets
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
    """One of the three H.264 pipelines (spec section 8.1), all constrained baseline from x264 with a one-second
    keyframe interval."""

    video_id: str = Field(description="The id viewers select the profile by")
    fps: int = Field(description="Frames per second")
    bitrate_kbps: int = Field(description="x264's target bitrate")
    speed_preset: str = Field(description="x264's speed preset")
    threads: int = Field(description="x264's thread count")


VIDEO_PROFILES: Final[tuple[VideoProfile, ...]] = (
    VideoProfile(video_id=VIDEO_ID_IDLE, fps=5, bitrate_kbps=800, speed_preset="ultrafast", threads=1),
    VideoProfile(video_id=VIDEO_ID_WATCH, fps=20, bitrate_kbps=2500, speed_preset="veryfast", threads=4),
    VideoProfile(video_id=VIDEO_ID_CONTROL, fps=30, bitrate_kbps=4000, speed_preset="veryfast", threads=4),
)


@pure
def _pipeline_config(profile: VideoProfile) -> dict[str, Any]:
    # neko evaluates each gst_params value as an expression: a bare word that names no variable is passed through
    # as it is, so a preset name works, and a value carrying ``=`` would not; none here does.
    return {
        "fps": str(profile.fps),
        "gst_prefix": "! video/x-raw,format=I420",
        "gst_encoder": "x264enc",
        "gst_params": {
            "threads": str(profile.threads),
            "bitrate": str(profile.bitrate_kbps),
            "key-int-max": str(profile.fps),
            "byte-stream": "true",
            "tune": "zerolatency",
            "speed-preset": profile.speed_preset,
        },
        "gst_suffix": "! video/x-h264,stream-format=byte-stream,profile=constrained-baseline",
        "show_pointer": False,
    }


@pure
def neko_config_document(secrets: NekoSecrets) -> dict[str, Any]:
    """neko's configuration as a document: loopback HTTP, the two ICE muxes on the published port, the three
    profiles, the multiuser provider with this start's passwords, the session rules of spec section 6.2."""
    return {
        "desktop": {
            "display": DISPLAY,
            "screen": f"{MAX_WIDTH}x{MAX_HEIGHT}@{MODE_REFRESH_HZ}",
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
                "codec": "h264",
                "ids": list(VIDEO_IDS),
                "pipelines": {profile.video_id: _pipeline_config(profile) for profile in VIDEO_PROFILES},
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
            "estimator": {"enabled": False},
        },
    }


@pure
def render_neko_yaml(secrets: NekoSecrets) -> str:
    return yaml.safe_dump(neko_config_document(secrets), sort_keys=False, default_flow_style=False)


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
