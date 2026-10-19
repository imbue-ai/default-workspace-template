import yaml
from inline_snapshot import snapshot
from pydantic import SecretStr

from screen.data_types import NekoSecrets
from screen.neko_config import neko_config_document
from screen.neko_config import render_neko_yaml
from screen.neko_config import render_pulse_script

_SECRETS = NekoSecrets(
    user_password=SecretStr("user-secret"), admin_password=SecretStr("admin-secret"), api_token=SecretStr("api-secret")
)


def test_neko_yaml_round_trips_and_carries_the_session_rules_and_pipelines() -> None:
    document = yaml.safe_load(render_neko_yaml(_SECRETS))
    assert document == neko_config_document(_SECRETS)
    assert document["session"] == snapshot(
        {
            "api_token": "api-secret",
            "implicit_hosting": False,
            "control_protection": False,
            "private_mode": False,
            "inactive_cursors": True,
            "merciful_reconnect": True,
            "locked_controls": False,
            "cookie": {"enabled": False, "secure": False},
        }
    )
    assert document["member"] == snapshot(
        {"provider": "multiuser", "multiuser": {"user_password": "user-secret", "admin_password": "admin-secret"}}
    )
    assert document["server"]["bind"] == "127.0.0.1:8091"
    assert document["webrtc"]["udpmux"] == 59000
    assert document["webrtc"]["tcpmux"] == 59000
    assert document["desktop"]["display"] == ":99"
    assert document["desktop"]["input"] == {"enabled": True, "socket": "/tmp/screen-runtime/xf86-input-neko.sock"}
    assert document["capture"]["video"]["ids"] == ["idle", "watch", "control"]
    assert document["capture"]["screencast"] == {"enabled": True, "rate": "5/1", "quality": "60"}
    assert document["capture"]["video"]["show_pointer"] is False
    assert document["capture"]["audio"]["device"] == "audio_output.monitor"


def test_video_pipelines_follow_the_profile_table() -> None:
    pipelines = neko_config_document(_SECRETS)["capture"]["video"]["pipelines"]
    assert {
        name: (
            pipeline["fps"],
            pipeline["gst_params"]["bitrate"],
            pipeline["gst_params"]["speed-preset"],
            pipeline["gst_params"]["threads"],
        )
        for name, pipeline in pipelines.items()
    } == snapshot(
        {
            "idle": ("5", "800", "ultrafast", "1"),
            "watch": ("20", "2500", "veryfast", "4"),
            "control": ("30", "4000", "veryfast", "4"),
        }
    )
    for pipeline in pipelines.values():
        assert pipeline["gst_encoder"] == "x264enc"
        assert pipeline["gst_params"]["tune"] == "zerolatency"
        assert pipeline["gst_params"]["key-int-max"] == pipeline["fps"]
        assert pipeline["show_pointer"] is False
        assert "constrained-baseline" in pipeline["gst_suffix"]
        assert not any("=" in value for value in pipeline["gst_params"].values())


def test_pulse_script_loads_the_null_sink_and_the_socket() -> None:
    assert render_pulse_script() == snapshot("""\
load-module module-null-sink sink_name=audio_output sink_properties=device.description="Screen_Output"
load-module module-native-protocol-unix socket=/tmp/screen-runtime/pulse.sock auth-anonymous=1
load-module module-always-sink
set-default-sink audio_output
""")
