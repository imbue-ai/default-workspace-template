from pathlib import Path

from imbue.imbue_common.ratchet_testing.ratchets import check_no_import_lint_errors

_DIR = Path(__file__).parent


def test_modules_import_only_downward_through_the_layers() -> None:
    check_no_import_lint_errors(_DIR, contract_name="workspace_themes layers")
