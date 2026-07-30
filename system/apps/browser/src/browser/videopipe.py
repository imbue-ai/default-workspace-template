"""H.264-over-WebSocket live video pipe (WebCodecs viewer prototype).

An alternative to the KasmVNC live view for A/B latency comparison: one ffmpeg
per viewer captures the browser's X display (x11grab), encodes with x264
ultrafast/zerolatency, and streams raw Annex B access units over the existing
browser-service WebSocket path. The client (assets/video.html) decodes with
WebCodecs -- no description blob is needed because WebCodecs treats a
description-less AVC config as Annex B, and every stream opens with SPS/PPS +
an IDR frame since the encoder is spawned per connection.

Why not reuse the Kasm relay or a new port: browser-service already serves
WebSockets through flask-sock on its registered service route, so the pipe
rides the same tunnel/dispatcher path the viewer page itself loads from.

Backpressure is the load-bearing design point. TCP delivery stalls must drop
frames here, on the server, or the socket buffer rebuilds exactly the
multi-second frame queue this prototype exists to eliminate: the reader thread
parses access units off ffmpeg's stdout into a small pending list, and when the
sender falls behind it discards delta frames wholesale and waits for the next
keyframe (a decoder can only rejoin at an IDR).
"""

import contextlib
import shutil
import subprocess
import threading
from typing import Any

from loguru import logger

# One frame is one Annex B access unit prefixed with a 1-byte key/delta flag;
# the client synthesizes timestamps from the frame rate.
FRAME_FLAG_KEY = b"\x01"
FRAME_FLAG_DELTA = b"\x00"

# Capture/encode settings. Baseline profile keeps the client codec string a
# constant ("avc1.42C028" -- baseline, level 4.0); zerolatency disables
# lookahead and B-frames so encode adds no frame of delay; scenecut=0 with a
# fixed keyint gives predictable IDR cadence for the drop-until-key recovery;
# aud=1 inserts access-unit delimiters, which is what lets the parser split
# frames without inspecting slice headers.
_FRAME_RATE = 24
_KEYINT = _FRAME_RATE * 4
# ponytail: fixed CRF, no bandwidth adaptation -- revisit if WAN tests show sustained overrun
_CRF = "28"

# Sender backlog (in access units) that triggers the drop-until-keyframe
# recovery; ~1/3 s at 24 fps. Small enough that a stall never queues a visible
# rewind, large enough to ride out normal send jitter.
_MAX_PENDING = 8

_NAL_AUD = 9
_NAL_IDR = 5


class VideoPipeError(RuntimeError):
    pass


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


def build_ffmpeg_command(display: str) -> list[str]:
    """The capture+encode pipeline for one viewer on one X display.

    No -video_size: x11grab then captures the display's size at spawn. A
    mid-stream display resize is NOT followed; the viewer reconnects (fresh
    ffmpeg) to pick up the new geometry.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "warning",
        "-f", "x11grab",
        "-framerate", str(_FRAME_RATE),
        "-i", display,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-pix_fmt", "yuv420p",
        "-crf", _CRF,
        "-g", str(_KEYINT),
        "-x264-params", "aud=1:scenecut=0",
        "-threads", "2",
        "-f", "h264",
        "-",
    ]


class AnnexBSplitter:
    """Incrementally split an Annex B byte stream into access units.

    Feed arbitrary chunks; get back complete access units. An AU is everything
    between two access-unit delimiter NALs (the encoder emits aud=1), with the
    delimiter kept at the front of its AU so SPS/PPS/IDR ordering inside the
    unit is untouched. An AU is a keyframe when it contains an IDR slice.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        # Byte offset of the AUD start code that opens the AU currently being
        # accumulated, or None until the first AUD is seen.
        self._open_au_start: int | None = None
        self._scan_from = 0

    def feed(self, chunk: bytes) -> list[tuple[bool, bytes]]:
        self._buffer.extend(chunk)
        units: list[tuple[bool, bytes]] = []
        found = self._find_next_aud()
        while found is not None:
            if self._open_au_start is None:
                self._open_au_start = found
            else:
                unit = bytes(self._buffer[self._open_au_start : found])
                units.append((self._contains_idr(unit), unit))
                del self._buffer[:found]
                self._open_au_start = 0
                self._scan_from = 0
            found = self._find_next_aud()
        return units

    def _find_next_aud(self) -> int | None:
        """Offset of the next unconsumed AUD NAL's start code, None until complete.

        Only ever returns an AUD strictly past the one opening the current unit,
        so ``feed`` always makes progress.
        """
        position = self._buffer.find(b"\x00\x00\x01", self._scan_from)
        while position != -1 and position + 3 < len(self._buffer):
            nal_start = position + 3
            nal_type = self._buffer[nal_start] & 0x1F
            # A 4-byte start code is a 3-byte one preceded by a zero.
            start = position - 1 if position > 0 and self._buffer[position - 1] == 0 else position
            self._scan_from = nal_start
            if nal_type == _NAL_AUD and (self._open_au_start is None or start > self._open_au_start):
                return start
            position = self._buffer.find(b"\x00\x00\x01", nal_start)
        # No further complete start code; rescan the unconsumed tail next feed
        # (a start code may straddle the chunk boundary).
        self._scan_from = max(0, len(self._buffer) - 4)
        return None

    @staticmethod
    def _contains_idr(unit: bytes) -> bool:
        position = unit.find(b"\x00\x00\x01")
        while position != -1 and position + 3 < len(unit):
            if unit[position + 3] & 0x1F == _NAL_IDR:
                return True
            position = unit.find(b"\x00\x00\x01", position + 3)
        return False


def stream_video(ws: Any, display: str, browser_id: str) -> None:
    """Run one viewer's capture->encode->send loop until the socket closes.

    Owns the ffmpeg process for this connection. The reader thread splits
    stdout into access units and appends them to the shared pending list under
    the condition; this (sender) thread drains it and sends. Drop policy on
    overflow: clear everything unsent and skip deltas until the next keyframe.
    """
    if not is_available():
        raise VideoPipeError("ffmpeg is not installed yet (env.d installs it with browser audio)")
    process = subprocess.Popen(
        build_ffmpeg_command(display),
        stdout=subprocess.PIPE,
        stderr=None,
        stdin=subprocess.DEVNULL,
    )
    stdout = process.stdout
    if stdout is None:
        raise VideoPipeError("ffmpeg stdout pipe was not created")
    pending: list[tuple[bool, bytes]] = []
    condition = threading.Condition()
    reader_done = threading.Event()

    def _read_units() -> None:
        splitter = AnnexBSplitter()
        need_key = False
        try:
            chunk = stdout.read(65536)
            while chunk:
                for is_key, unit in splitter.feed(chunk):
                    with condition:
                        if len(pending) >= _MAX_PENDING:
                            logger.debug("video pipe {} dropping {} stale frames", browser_id, len(pending))
                            pending.clear()
                            need_key = True
                        if need_key:
                            if not is_key:
                                continue
                            need_key = False
                        pending.append((is_key, unit))
                        condition.notify()
                chunk = stdout.read(65536)
        finally:
            reader_done.set()
            with condition:
                condition.notify()

    reader = threading.Thread(target=_read_units, name=f"video-pipe-{browser_id}", daemon=True)
    reader.start()
    logger.info("video pipe started for browser {} on {}", browser_id, display)
    try:
        # The unlocked read of `pending` only decides whether to take the lock
        # again; the authoritative check happens under the condition.
        while pending or not reader_done.is_set():
            with condition:
                while not pending and not reader_done.is_set():
                    condition.wait(timeout=1.0)
                if not pending:
                    continue
                is_key, unit = pending.pop(0)
            ws.send((FRAME_FLAG_KEY if is_key else FRAME_FLAG_DELTA) + unit)
    finally:
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        reader.join(timeout=3)
        logger.info("video pipe stopped for browser {}", browser_id)
