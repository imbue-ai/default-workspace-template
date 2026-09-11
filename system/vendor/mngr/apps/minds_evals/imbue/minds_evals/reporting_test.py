from pathlib import Path

import pytest

from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import write_reports


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("a | b", "a \\| b"),
        ("first\nsecond", "first second"),
        ("a|b\nc", "a\\|b c"),
        ("", ""),
    ],
)
def test_as_table_cell_keeps_free_text_inside_its_cell(text: str, expected: str) -> None:
    """A pipe ends the cell and a newline ends the row, so a ref, a config path or an exception
    message carrying either would silently rewrite the table around it."""
    assert as_table_cell(text) == expected


def test_write_reports_creates_the_directory_and_skips_the_reports_not_asked_for(tmp_path: Path) -> None:
    written_path = tmp_path / "nested" / "summary.md"

    write_reports([(written_path, "content\n"), (None, "never written")])

    assert written_path.read_text() == "content\n"
    assert list(tmp_path.iterdir()) == [tmp_path / "nested"]
