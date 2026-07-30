"""On-demand Opus audio for the streamed browser (pcmflux).

pcmflux is linuxserver's audio sibling of pixelflux (same vendor, same wheel
family) -- it captures a PulseAudio source with ``pa_simple``, Opus-encodes on
a native thread, and invokes a Python callback per encoded chunk, exactly the
shape ``videopipe`` already uses.

Only-when-playing, for free: pcmflux's ``use_silence_gate`` compares each 20ms
frame against digital zero and skips both the encode and the callback when the
tab is silent. A silent browser therefore costs zero encode CPU and zero bytes
on the wire; the only residual is pcmflux reading zeros off the null sink's
monitor (a sub-1%-of-a-core memcpy). No pactl-subscribe process, no ffmpeg, no
relay -- the gate is sample-accurate, so it also catches the common case of a
site holding an uncorked-but-silent audio stream.

Chunks ride the SAME ``/stream`` WebSocket as video, interleaved by a leading
magic byte 0x01 (video packets lead with 0x04); the raw Opus payload follows.
They never touch the video credit window and are dropped, never queued, when
the link stalls -- audio staleness self-bounds at the client's jitter buffer.
"""

import importlib
import os
import threading
from collections import deque
from typing import Any

from loguru import logger

# Same retryable-import guard as videopipe: the native module dlopens system
# libraries that env-converge may still be installing when the service first
# boots, and an unguarded module-level import would crash-loop the service.
_pcmflux: dict[str, object] = {"module": None, "error": "not yet imported"}


def _attempt_pcmflux_import() -> None:
    if _pcmflux["module"] is not None:
        return
    try:
        _pcmflux["module"] = importlib.import_module("pcmflux")
    except ImportError as error:
        _pcmflux["error"] = str(error)
        return
    _pcmflux["error"] = None


_attempt_pcmflux_import()

AUDIO_MAGIC = 0x01
_SAMPLE_RATE = 48000
_CHANNELS = 2
_OPUS_BITRATE = int(os.environ.get("BROWSER_AUDIO_BITRATE", "96000"))
_FRAME_MS = 20
# Newest-wins backlog cap (~320ms). A stalled link drops oldest here rather than
# queueing latency; the client's worklet has its own bounded jitter buffer.
_MAX_QUEUED_CHUNKS = 16


class AudioPipeError(RuntimeError):
    pass


def is_available() -> bool:
    return _pcmflux["module"] is not None


class AudioPipe:
    """One viewer's Opus capture, gated on real audio, mailboxed for the sender.

    Shares the video pipe's Condition so a fresh chunk wakes the single sender
    thread (simple_websocket sends are not cross-thread safe, so ALL sends --
    video, cursor, control, audio -- run on that one thread).
    """

    def __init__(self, source_device: str, condition: threading.Condition) -> None:
        self._source = source_device
        self._condition = condition
        self._capture = None
        self._chunks: deque[bytes] = deque(maxlen=_MAX_QUEUED_CHUNKS)
        self.frames_sent = 0

    def start(self) -> None:
        _attempt_pcmflux_import()
        if _pcmflux["module"] is None:
            raise AudioPipeError(f"pcmflux failed to import: {_pcmflux['error']}")
        pcmflux_module: Any = _pcmflux["module"]
        settings = pcmflux_module.AudioCaptureSettings()
        settings.device_name = self._source.encode() if isinstance(self._source, str) else self._source
        settings.sample_rate = _SAMPLE_RATE
        settings.channels = _CHANNELS
        settings.opus_bitrate = _OPUS_BITRATE
        settings.frame_duration_ms = _FRAME_MS
        settings.use_vbr = True
        settings.use_silence_gate = True  # the entire only-when-playing story
        settings.omit_audio_header = True  # we prepend our own 1-byte magic
        settings.red_distance = 0
        capture = pcmflux_module.AudioCapture()
        capture.start_capture(settings, self._on_chunk)
        self._capture = capture
        logger.info("audio pipe started on {} (opus {}bps, silence-gated)", self._source, _OPUS_BITRATE)

    def _on_chunk(self, frame) -> None:  # noqa: ANN001  (pcmflux native frame)
        # Native delivery thread: only fires for non-silent frames (the gate).
        payload = bytes([AUDIO_MAGIC]) + bytes(frame)
        with self._condition:
            self._chunks.append(payload)
            self._condition.notify()

    def has_pending(self) -> bool:
        return bool(self._chunks)

    def drain(self) -> list[bytes]:
        """All queued chunks, oldest-first (caller holds nothing; sender thread)."""
        with self._condition:
            out = list(self._chunks)
            self._chunks.clear()
        self.frames_sent += len(out)
        return out

    def stop(self) -> None:
        capture, self._capture = self._capture, None
        if capture is None:
            return
        # stop_capture joins native threads; guard against a wedge taking the
        # connection handler down (same pattern as videopipe).
        stopper = threading.Thread(target=lambda: _stop_capture(capture), daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        if stopper.is_alive():
            logger.warning("audio pipe capture did not stop within 5s; abandoning it")


def _stop_capture(capture) -> None:  # noqa: ANN001
    try:
        capture.stop_capture()
    except Exception:  # noqa: BLE001  (teardown of a native handle must never raise up)
        logger.debug("audio stop_capture raised during teardown")
