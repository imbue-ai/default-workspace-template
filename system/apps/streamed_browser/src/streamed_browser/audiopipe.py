"""On-demand MPEG-TS/MP2 audio for the streamed browser (ffmpeg -> jsmpeg).

The client plays audio with jsmpeg, a proven library that owns the demux,
decode, and WebAudio scheduling (no hand-written jitter buffer -- the reason
this replaced a WebCodecs/Opus attempt). So the server just has to produce a
clean MPEG-TS/MP2 byte stream and pump it down a dedicated ``/audio`` socket.

Only-when-playing, for CPU: ffmpeg runs ONLY while a PulseAudio sink-input is
actively playing (uncorked -- PulseAudio corks a stream when its producer goes
idle). A ``pactl subscribe`` event stream (not a poll) drives a reconcile that
starts ffmpeg when sound begins and kills it when it stops -- so a silent tab
costs nothing (no encoder process at all).
"""

import json
import os
import shutil
import subprocess
import threading

from loguru import logger

_PULSE_SERVER = "unix:/var/run/pulse/native"
# ffmpeg: capture the null sink's monitor, MP2 in MPEG-TS to stdout. mono +
# 128k keeps it cheap; muxdelay ~0 and a small fragment keep latency low. These
# are the workspace browser app's proven values.
_FFMPEG_ARGS = (
    "-nostdin", "-loglevel", "warning",
    "-f", "pulse", "-fragment_size", "2000", "-ar", "44100", "-i", "{source}",
    "-f", "mpegts", "-codec:a", "mp2", "-b:a", "128k", "-ac", "1", "-muxdelay", "0.001",
    "pipe:1",
)


class AudioPipeError(RuntimeError):
    pass


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("pactl") is not None


class AudioStreamer:
    """One viewer's gated ffmpeg -> MPEG-TS pump.

    ``on_data`` is called with each ffmpeg stdout chunk (the caller sends it on
    the /audio socket). Everything is torn down by ``stop``.
    """

    def __init__(self, source_device: str, on_data) -> None:  # noqa: ANN001  (bytes callback)
        self._source = source_device
        self._on_data = on_data
        self._env = {**os.environ, "PULSE_SERVER": _PULSE_SERVER}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._subscribe: subprocess.Popen[str] | None = None
        self._monitor: threading.Thread | None = None

    def start(self) -> None:
        if not is_available():
            raise AudioPipeError("ffmpeg/pactl not installed")
        self._monitor = threading.Thread(target=self._monitor_loop, name="audio-gate", daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        # React to sink-input state changes (event-driven, no poll). Reconcile
        # once up front so audio already playing at connect starts immediately.
        self._reconcile()
        try:
            proc = subprocess.Popen(
                ["pactl", "subscribe"], env=self._env,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except OSError as error:
            logger.warning("audio gate could not start pactl subscribe ({})", error)
            return
        self._subscribe = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            if self._stop.is_set():
                break
            if "sink-input" in line:
                self._reconcile()

    def _sink_active(self) -> bool:
        try:
            result = subprocess.run(
                ["pactl", "-f", "json", "list", "sink-inputs"],
                env=self._env, capture_output=True, text=True, timeout=5,
            )
            inputs = json.loads(result.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        # "Sound is playing" == at least one uncorked sink-input. PulseAudio corks
        # a stream when its producer goes idle (Chromium corks a silent tab), so
        # an uncorked input means audio is actively flowing into the sink. We key
        # on `corked` rather than the sink-input `state` field because this
        # PulseAudio build does not emit `state` in JSON at all (it comes back
        # absent -> the old `state == "RUNNING"` test was always False, so ffmpeg
        # never started and no audio was ever encoded). Absent `corked` defaults
        # to True (treat as inactive) so a missing field can't spuriously encode.
        return any(not item.get("corked", True) for item in inputs)

    def _reconcile(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            active = self._sink_active()
            running = self._ffmpeg is not None and self._ffmpeg.poll() is None
            if active and not running:
                self._start_ffmpeg_locked()
            elif not active and running:
                self._stop_ffmpeg_locked()

    def _start_ffmpeg_locked(self) -> None:
        args = ["ffmpeg", *(a.format(source=self._source) for a in _FFMPEG_ARGS)]
        self._ffmpeg = subprocess.Popen(
            args, env=self._env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._reader = threading.Thread(target=self._pump, args=(self._ffmpeg,), name="audio-pump", daemon=True)
        self._reader.start()
        logger.info("audio: sound started, encoding")

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        while not self._stop.is_set():
            chunk = stdout.read(4096)
            if not chunk:
                break
            self._on_data(chunk)

    def _stop_ffmpeg_locked(self) -> None:
        proc, self._ffmpeg = self._ffmpeg, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("audio: sound stopped, encoder off")

    def stop(self) -> None:
        self._stop.set()
        if self._subscribe is not None:
            self._subscribe.terminate()
        with self._lock:
            self._stop_ffmpeg_locked()
