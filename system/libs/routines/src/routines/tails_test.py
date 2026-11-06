from pathlib import Path

from routines.tails import read_tail


def test_the_tail_is_the_whole_of_a_short_file_and_the_end_of_a_long_one(tmp_path: Path) -> None:
    short = tmp_path / "short"
    short.write_bytes(b"abc")
    assert read_tail(short, 10) == ("abc", False)

    long = tmp_path / "long"
    long.write_bytes(b"0123456789")
    assert read_tail(long, 4) == ("6789", True)


def test_bytes_that_are_not_utf8_are_replaced_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "raw"
    path.write_bytes(b"ok \xff")
    text, _ = read_tail(path, 10)
    assert text == "ok \ufffd"
