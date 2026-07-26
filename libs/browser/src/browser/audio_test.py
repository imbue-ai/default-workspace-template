"""Unit tests for the audio silence gate and PCM fan-out (no ffmpeg / PulseAudio needed --
the fan-out path is exercised directly with fake chunks, mirroring capture_test)."""

import queue
import struct

from browser.audio import _CHUNK_BYTES, AudioCapture, _is_silent


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_is_silent_true_for_zeros() -> None:
    assert _is_silent(_pcm([0, 0, 0, 0]))


def test_is_silent_true_below_threshold() -> None:
    assert _is_silent(_pcm([1, -2, 3, -4, 20, -24]))


def test_is_silent_false_when_any_sample_loud() -> None:
    assert not _is_silent(_pcm([0, 0, 5000, 0]))
    assert not _is_silent(_pcm([-30000]))


def test_is_silent_handles_empty() -> None:
    assert _is_silent(b"")


def test_chunk_bytes_is_20ms_stereo_s16() -> None:
    # 48000 Hz * 20 ms * 2 channels * 2 bytes = 3840; the client decodes this exact framing.
    assert _CHUNK_BYTES == 3840


def test_fan_out_delivers_to_every_subscriber() -> None:
    cap = AudioCapture(["-f", "lavfi", "-i", "sine"])
    a: "queue.Queue[bytes | None]" = queue.Queue(maxsize=4)
    b: "queue.Queue[bytes | None]" = queue.Queue(maxsize=4)
    cap._subscribers = [a, b]
    cap._fan_out(b"pcm-chunk")
    assert a.get_nowait() == b"pcm-chunk"
    assert b.get_nowait() == b"pcm-chunk"


def test_fan_out_drops_oldest_when_a_subscriber_is_full() -> None:
    cap = AudioCapture(["-f", "lavfi", "-i", "sine"])
    slow: "queue.Queue[bytes | None]" = queue.Queue(maxsize=2)
    cap._subscribers = [slow]
    for i in range(5):
        cap._fan_out(bytes([i]))
    # Only the two NEWEST chunks survive; the oldest were dropped to stay live.
    assert [slow.get_nowait(), slow.get_nowait()] == [bytes([3]), bytes([4])]
    assert slow.empty()


def test_has_subscribers_tracks_the_list() -> None:
    cap = AudioCapture(["-f", "lavfi", "-i", "sine"])
    assert not cap.has_subscribers()
    q: "queue.Queue[bytes | None]" = queue.Queue()
    cap._subscribers = [q]
    assert cap.has_subscribers()


def test_close_sentinels_subscribers_and_clears() -> None:
    cap = AudioCapture(["-f", "lavfi", "-i", "sine"])
    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=2)
    cap._subscribers = [q]
    cap.close()  # no proc running -> just drains subscribers
    assert q.get_nowait() is None  # sentinel so the socket loop ends
    assert not cap.has_subscribers()
