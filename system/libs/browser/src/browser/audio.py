"""On-demand audio capture for one browser: its PulseAudio sink -> PCM over a socket.

The video path (``capture.py``) streams the screen; this streams the SOUND, the same
way: each browser plays into its OWN PulseAudio null sink (the audio analog of its own
Xvfb display -- see ``display.py`` / ``session.py``), and we capture that sink's monitor
with ``ffmpeg`` into raw little-endian PCM (48 kHz, stereo, s16). One :class:`AudioCapture`
per browser, started ON DEMAND -- the first ``/audio`` subscriber starts ffmpeg, the last
to leave stops it, so an unwatched (or muted-pane) browser costs nothing.

Wire format: raw interleaved PCM frames, no header or container -- the client knows the
fixed format and plays them through a Web Audio ring buffer. Near-silent chunks are
dropped (a browser is silent most of the time), so the ~1.5 Mbps only flows while sound is
actually playing; the client's buffer simply underruns to silence in the gaps.

Threading mirrors :class:`capture.Capture`: a reader thread pulls fixed PCM chunks off
ffmpeg's stdout and fans them out to per-subscriber queues; start/stop run on the daemon's
loop thread, and the blocking ffmpeg teardown is punted to a short-lived daemon thread so a
wedged pipe can't freeze the fleet.
"""

import os
import queue
import subprocess
import threading

from loguru import logger

_AUDIO_RATE = int(os.environ.get("BROWSER_AUDIO_RATE", "48000"))
_AUDIO_CHANNELS = 2
_BYTES_PER_SAMPLE = 2  # s16le
# ~20 ms per chunk: small enough for low latency, big enough that per-chunk overhead is
# negligible. read() blocks until a full chunk is available (ffmpeg --flush_packets keeps
# it flowing), so chunks arrive at a steady ~50/s while sound plays.
_CHUNK_FRAMES = _AUDIO_RATE * 20 // 1000
_CHUNK_BYTES = _CHUNK_FRAMES * _AUDIO_CHANNELS * _BYTES_PER_SAMPLE
# Bound the per-subscriber queue so a stalled socket can't buffer unbounded audio latency:
# at ~20 ms/chunk, 32 chunks ≈ 640 ms, and we drop the OLDEST on overflow to stay live.
_AUDIO_QUEUE_MAX = int(os.environ.get("BROWSER_AUDIO_QUEUE_MAX", "32"))
# Peak |sample| (of 32767) below which a chunk is treated as digital silence and dropped.
# Low, so only true silence is skipped -- quiet audio still streams.
_SILENCE_PEAK = int(os.environ.get("BROWSER_AUDIO_SILENCE_PEAK", "24"))

_FFMPEG_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def create_null_sink(name: str) -> str | None:
    """Create a per-browser PulseAudio null sink and return its module id (for teardown),
    or None if PulseAudio isn't reachable. The browser's Chromium is pointed at this sink
    (PULSE_SINK) so each browser's audio is ISOLATED -- viewing browser A never plays
    browser B's sound -- exactly like each browser having its own Xvfb display. Capture
    reads ``<name>.monitor``. Best-effort: no sink -> no audio, never a failed launch."""
    try:
        result = subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             f"sink_name={name}", f"sink_properties=device.description={name}"],
            capture_output=True, text=True, timeout=5,
        )
    except _FFMPEG_ERRORS as e:
        logger.warning("pactl unavailable ({}); no audio for this browser", e)
        return None
    if result.returncode != 0:
        logger.warning("could not create audio sink {} ({}); no audio", name, result.stderr.strip())
        return None
    return result.stdout.strip()


def remove_null_sink(module_id: str) -> None:
    """Unload a null sink created by :func:`create_null_sink`. Best-effort."""
    try:
        subprocess.run(["pactl", "unload-module", module_id], capture_output=True, timeout=5)
    except _FFMPEG_ERRORS as e:
        logger.debug("pactl unload-module {} ignored ({})", module_id, e)


def _is_silent(chunk: bytes) -> bool:
    """True if every s16 sample in ``chunk`` is within ``_SILENCE_PEAK`` of zero.
    Pure stdlib (memoryview.cast) so audio.py pulls in no array dependency."""
    try:
        samples = memoryview(chunk).cast("h")  # little-endian int16 on x86/ARM
    except (TypeError, ValueError):
        return False
    peak = 0
    for s in samples:
        a = s if s >= 0 else -s
        if a > peak:
            peak = a
            if peak > _SILENCE_PEAK:
                return False
    return True


class AudioCapture:
    """On-demand ffmpeg capture of one browser's PulseAudio sink into PCM."""

    def __init__(self, input_args: list[str]) -> None:
        # The ffmpeg INPUT spec, e.g. ["-f", "pulse", "-i", "mind_100.monitor"]. Injected
        # (not hardcoded) so tests can drive a synthetic "-f lavfi -i sine=..." source with
        # no PulseAudio present.
        self._input_args = input_args
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribers: list["queue.Queue[bytes | None]"] = []
        self._lock = threading.Lock()  # guards _subscribers + _proc against the reader thread

    def add_subscriber(self) -> "queue.Queue[bytes | None] | None":
        """Register an ``/audio`` socket, starting ffmpeg on the first subscriber. Returns
        its outbound queue, or None if ffmpeg won't start (missing binary / bad source) so
        the caller closes the socket and the viewer's backoff retries."""
        client_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)
        with self._lock:
            if not self._subscribers and not self._start_locked():
                return None
            self._subscribers.append(client_queue)
        return client_queue

    def remove_subscriber(self, client_queue: "queue.Queue[bytes | None]") -> None:
        """Deregister; stop ffmpeg when the last subscriber leaves. The blocking teardown
        runs OUTSIDE the lock (and off the loop thread) so it can't wedge the fleet."""
        proc_to_stop = None
        reader_to_join = None
        with self._lock:
            if client_queue in self._subscribers:
                self._subscribers.remove(client_queue)
            if not self._subscribers and self._proc is not None:
                self._stop.set()
                proc_to_stop, self._proc = self._proc, None
                reader_to_join, self._reader = self._reader, None
        if proc_to_stop is not None:
            self._stop_ffmpeg(proc_to_stop, reader_to_join)

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def _start_locked(self) -> bool:
        """Spawn ffmpeg capturing the sink into raw PCM on stdout. Returns False (no audio,
        never a crash) if it can't launch."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            *self._input_args,
            "-ac", str(_AUDIO_CHANNELS), "-ar", str(_AUDIO_RATE),
            "-f", "s16le", "-flush_packets", "1", "pipe:1",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except _FFMPEG_ERRORS as e:
            logger.warning("audio ffmpeg failed to start ({}); no sound for this browser", e)
            return False
        self._proc = proc
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), name="audio-read", daemon=True)
        self._reader.start()
        logger.info("browser audio: capture started ({} Hz stereo)", _AUDIO_RATE)
        return True

    def _read_loop(self, proc: "subprocess.Popen[bytes]") -> None:
        """Pull fixed PCM chunks off ffmpeg and fan out, dropping silence and shedding the
        oldest queued chunk on a slow subscriber (stay live, never accumulate latency)."""
        stdout = proc.stdout
        assert stdout is not None
        while not self._stop.is_set():
            try:
                chunk = stdout.read(_CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break  # ffmpeg exited / pipe closed
            if len(chunk) < _CHUNK_BYTES or _is_silent(chunk):
                continue
            self._fan_out(chunk)

    def _fan_out(self, chunk: bytes) -> None:
        """Deliver one PCM chunk to every subscriber, dropping the OLDEST queued chunk on a
        full (slow) subscriber so audio latency stays bounded -- never accumulates."""
        with self._lock:
            subscribers = list(self._subscribers)
        for client_queue in subscribers:
            try:
                client_queue.put_nowait(chunk)
            except queue.Full:
                try:
                    client_queue.get_nowait()
                    client_queue.put_nowait(chunk)
                except (queue.Empty, queue.Full):
                    pass

    def _stop_ffmpeg(self, proc: "subprocess.Popen[bytes]", reader: threading.Thread | None) -> None:
        """Terminate ffmpeg in a short-lived daemon thread (proc.wait can block on a wedged
        pipe; keep it off the loop thread, like capture.py's stop)."""
        def _kill() -> None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            except _FFMPEG_ERRORS as e:
                logger.debug("audio ffmpeg stop ignored ({})", e)
            if reader is not None:
                reader.join(timeout=2)
        threading.Thread(target=_kill, name="audio-stop", daemon=True).start()
        logger.info("browser audio: capture stopping")

    def close(self) -> None:
        """Tear down: drop subscribers (sentinel each so its socket loop ends) and stop
        ffmpeg. Idempotent."""
        proc_to_stop = None
        reader_to_join = None
        with self._lock:
            for client_queue in self._subscribers:
                try:
                    client_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()
            if self._proc is not None:
                self._stop.set()
                proc_to_stop, self._proc = self._proc, None
                reader_to_join, self._reader = self._reader, None
        if proc_to_stop is not None:
            self._stop_ffmpeg(proc_to_stop, reader_to_join)
