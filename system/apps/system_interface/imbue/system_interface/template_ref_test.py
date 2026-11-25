import subprocess
from pathlib import Path

from imbue.system_interface.template_ref import read_template_ref

_MARKER = "update-self: merge upstream template ({})"


def _git(repo_root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def test_a_checkout_is_read_by_its_tag_and_then_by_its_update_self_merge(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "commit", "--allow-empty", "--quiet", "-m", "the template at a release")
    _git(tmp_path, "tag", "minds-v0.9.0")

    assert read_template_ref(tmp_path) == "minds-v0.9.0"

    _git(tmp_path, "commit", "--allow-empty", "--quiet", "-m", _MARKER.format("minds-v0.10.2"))

    assert read_template_ref(tmp_path) == "minds-v0.10.2"


def test_a_directory_that_is_no_checkout_names_no_version(tmp_path: Path) -> None:
    assert read_template_ref(tmp_path) == ""
