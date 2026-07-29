"""Per-browser PulseAudio sink and Kasm MPEG audio relay."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger

_INSTALL_DIR = Path(os.environ.get("BROWSER_AUDIO_INSTALL_DIR", "/opt/kasm-audio"))
JSMPEG_CLIENT = _INSTALL_DIR / "jsmpeg.min.js"
_RELAY = _INSTALL_DIR / "kasm_audio_out-linux"
_PULSE_SERVER = os.environ.get("BROWSER_PULSE_SERVER", "unix:/var/run/pulse/native")
_INGEST_BASE = int(os.environ.get("BROWSER_AUDIO_INGEST_PORT_BASE", "7000"))
_WEBSOCKET_BASE = int(os.environ.get("BROWSER_AUDIO_WEBSOCKET_PORT_BASE", "7100"))
_READY_TIMEOUT = 10.0
_STOP_TIMEOUT = 3.0
_pulse_lock = threading.Lock()


class AudioStartupError(RuntimeError):
    pass


def is_available() -> bool:
    return bool(
        shutil.which("pulseaudio")
        and shutil.which("pactl")
        and shutil.which("ffmpeg")
        and _RELAY.is_file()
        and JSMPEG_CLIENT.is_file()
    )


def service_name_for(browser_id: str) -> str:
    return f"browser-{browser_id}-audio"


def _pulse_env() -> dict[str, str]:
    return {**os.environ, "PULSE_SERVER": _PULSE_SERVER}


def _port_is_listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _stop_process(
    process: subprocess.Popen[bytes] | subprocess.Popen[str] | None,
) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_STOP_TIMEOUT)


def _ensure_pulse() -> None:
    with _pulse_lock:
        if (
            subprocess.run(
                ["pactl", "info"], env=_pulse_env(), capture_output=True, timeout=5
            ).returncode
            == 0
        ):
            return
        subprocess.run(
            [
                "pulseaudio",
                "--system",
                "--daemonize=yes",
                "--disallow-exit",
                "--exit-idle-time=-1",
            ],
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if (
                subprocess.run(
                    ["pactl", "info"], env=_pulse_env(), capture_output=True, timeout=5
                ).returncode
                == 0
            ):
                return
            threading.Event().wait(0.1)
        raise AudioStartupError("PulseAudio did not become ready")


class BrowserAudio:
    """Audio resources for one fleet browser; capture follows visible listeners."""

    def __init__(self, browser_id: str, slot: int, certificate: Path) -> None:
        self.browser_id = browser_id
        self.sink_name = "browser_" + browser_id.replace("-", "_")
        self.ingest_port = _INGEST_BASE + slot
        self.websocket_port = _WEBSOCKET_BASE + slot
        self.certificate = certificate
        self._module_index: int | None = None
        self._sink_index: int | None = None
        self._relay: subprocess.Popen[bytes] | None = None
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._subscription: subprocess.Popen[str] | None = None
        self._viewers = 0
        self._lock = threading.Lock()

    @property
    def environment(self) -> dict[str, str]:
        return {"PULSE_SERVER": _PULSE_SERVER, "PULSE_SINK": self.sink_name}

    def start(self) -> None:
        if not is_available():
            raise AudioStartupError("browser audio dependencies are not installed yet")
        if not self.certificate.is_file():
            raise AudioStartupError(
                f"KasmVNC certificate is missing: {self.certificate}"
            )
        _ensure_pulse()
        loaded = subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={self.sink_name}",
                "rate=44100",
                "channels=2",
            ],
            env=_pulse_env(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self._module_index = int(loaded.stdout.strip())
        self._sink_index = self._find_sink_index()
        self._relay = subprocess.Popen(
            [
                str(_RELAY),
                "kasmaudio",
                str(self.ingest_port),
                str(self.websocket_port),
                str(self.certificate),
                str(self.certificate),
            ],
            stdout=None,
            stderr=None,
        )
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._relay.poll() is not None:
                code = self._relay.returncode
                self.stop()
                raise AudioStartupError(f"Kasm audio relay exited with {code}")
            if _port_is_listening(self.websocket_port):
                self._forward_port(
                    [
                        "--name",
                        service_name_for(self.browser_id),
                        "--url",
                        f"https://localhost:{self.websocket_port}",
                    ]
                )
                return
            threading.Event().wait(0.1)
        self.stop()
        raise AudioStartupError("Kasm audio relay did not become ready")

    def _pactl_json(self, *args: str) -> list[dict[str, object]]:
        result = subprocess.run(
            ["pactl", "-f", "json", *args],
            env=_pulse_env(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = json.loads(result.stdout or "[]")
        return value if isinstance(value, list) else []

    def _find_sink_index(self) -> int:
        for sink in self._pactl_json("list", "sinks"):
            if sink.get("name") == self.sink_name:
                index = sink.get("index")
                if isinstance(index, (int, str)):
                    return int(index)
        raise AudioStartupError(f"Pulse sink {self.sink_name} was not created")

    def _sink_active(self) -> bool:
        try:
            return any(
                item.get("sink") == self._sink_index and item.get("state") == "RUNNING"
                for item in self._pactl_json("list", "sink-inputs")
            )
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            logger.debug(
                "audio activity check failed for {} ({})", self.browser_id, error
            )
            return False

    def connect(self) -> None:
        with self._lock:
            if self._viewers:
                return
            self._viewers = 1
            self._subscription = subprocess.Popen(
                ["pactl", "subscribe"],
                env=_pulse_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            threading.Thread(
                target=self._monitor_activity,
                name=f"audio-{self.browser_id}",
                daemon=True,
            ).start()

    def disconnect(self) -> None:
        with self._lock:
            self._viewers = 0
            subscription, self._subscription = self._subscription, None
            _stop_process(subscription)
            self._stop_capture_locked()

    def _monitor_activity(self) -> None:
        self._reconcile_capture()
        subscription = self._subscription
        if subscription is None or subscription.stdout is None:
            return
        for _line in subscription.stdout:
            self._reconcile_capture()

    def _reconcile_capture(self) -> None:
        active = self._sink_active()
        with self._lock:
            if (
                self._viewers
                and active
                and (self._ffmpeg is None or self._ffmpeg.poll() is not None)
            ):
                self._ffmpeg = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-loglevel",
                        "warning",
                        "-f",
                        "pulse",
                        "-fragment_size",
                        "2000",
                        "-ar",
                        "44100",
                        "-i",
                        f"{self.sink_name}.monitor",
                        "-f",
                        "mpegts",
                        "-codec:a",
                        "mp2",
                        "-b:a",
                        "128k",
                        "-ac",
                        "1",
                        "-muxdelay",
                        "0.001",
                        f"http://127.0.0.1:{self.ingest_port}/kasmaudio",
                    ],
                    env=_pulse_env(),
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                )
            elif (not self._viewers or not active) and self._ffmpeg is not None:
                self._stop_capture_locked()

    def _stop_capture_locked(self) -> None:
        process, self._ffmpeg = self._ffmpeg, None
        _stop_process(process)

    def stop(self) -> None:
        self._forward_port(["--remove", "--name", service_name_for(self.browser_id)])
        with self._lock:
            self._viewers = 0
            subscription, self._subscription = self._subscription, None
            _stop_process(subscription)
            self._stop_capture_locked()
        _stop_process(self._relay)
        self._relay = None
        if self._module_index is not None:
            subprocess.run(
                ["pactl", "unload-module", str(self._module_index)],
                env=_pulse_env(),
                capture_output=True,
                timeout=5,
            )
            self._module_index = None

    def _forward_port(self, args: list[str]) -> None:
        script = Path(__file__).resolve().parents[4] / "scripts" / "forward_port.py"
        try:
            subprocess.run(
                ["python3", str(script), *args],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.error(
                "audio forward_port failed for {} ({})", self.browser_id, error
            )
