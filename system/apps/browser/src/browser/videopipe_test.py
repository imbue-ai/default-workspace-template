"""Unit tests for the Annex B access-unit splitter behind the video pipe."""

from browser.videopipe import AnnexBSplitter, build_ffmpeg_command


def _nal(nal_type: int, payload: bytes = b"\x00") -> bytes:
    return b"\x00\x00\x00\x01" + bytes([nal_type]) + payload


_AUD = _nal(9)
_SPS = _nal(7, b"\x42\xc0\x28")
_PPS = _nal(8)
_IDR = _nal(5, b"\x11" * 8)
_DELTA = _nal(1, b"\x22" * 8)


def test_splits_units_on_access_unit_delimiters() -> None:
    stream = _AUD + _SPS + _PPS + _IDR + _AUD + _DELTA + _AUD + _DELTA
    units = AnnexBSplitter().feed(stream)
    # The third AUD opens a still-unterminated unit, so exactly two complete units.
    assert [is_key for is_key, _ in units] == [True, False]
    assert units[0][1] == _AUD + _SPS + _PPS + _IDR
    assert units[1][1] == _AUD + _DELTA


def test_reassembles_units_across_arbitrary_chunk_boundaries() -> None:
    stream = (_AUD + _SPS + _PPS + _IDR + _AUD + _DELTA) * 3 + _AUD
    for chunk_size in (1, 2, 3, 5, 7, len(stream)):
        splitter = AnnexBSplitter()
        units = []
        for start in range(0, len(stream), chunk_size):
            units.extend(splitter.feed(stream[start : start + chunk_size]))
        assert [is_key for is_key, _ in units] == [True, False] * 3, f"chunk_size={chunk_size}"
        assert b"".join(unit for _, unit in units) + _AUD == stream, f"chunk_size={chunk_size}"


def test_ignores_bytes_before_the_first_delimiter() -> None:
    units = AnnexBSplitter().feed(b"\x00\x00\x00\x01\x06garbage" + _AUD + _DELTA + _AUD)
    assert [is_key for is_key, _ in units] == [False]
    assert units[0][1] == _AUD + _DELTA


def test_ffmpeg_command_targets_the_display() -> None:
    command = build_ffmpeg_command(":37")
    assert ":37" in command
    assert command[command.index("-f") + 1] == "x11grab"
