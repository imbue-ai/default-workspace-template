from pathlib import Path

from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors

_DIR = Path(__file__).parent


def test_no_type_errors() -> None:
    check_no_type_errors(_DIR)
