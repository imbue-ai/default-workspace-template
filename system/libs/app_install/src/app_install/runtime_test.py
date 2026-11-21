from pathlib import Path

import pytest

from app_install.runtime import has_test_files
from app_install.testing import write_workspace_file


@pytest.mark.parametrize("test_file", ["src/notes/server_test.py", "test_notes_ratchets.py"])
def test_an_app_with_a_test_file_of_its_own_has_tests_to_run(tmp_path: Path, test_file: str) -> None:
    write_workspace_file(tmp_path, "src/notes/server.py")
    write_workspace_file(tmp_path, test_file)

    assert has_test_files(tmp_path)


def test_the_tests_of_the_packages_an_app_installs_are_not_its_own(tmp_path: Path) -> None:
    write_workspace_file(tmp_path, "src/notes/server.py")
    write_workspace_file(tmp_path, ".venv/lib/python3.12/site-packages/flask/test_flask.py")
    write_workspace_file(tmp_path, "frontend/node_modules/some-package/helper_test.py")

    assert not has_test_files(tmp_path)
