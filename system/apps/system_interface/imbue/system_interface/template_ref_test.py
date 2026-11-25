import subprocess
from pathlib import Path

import pytest

from imbue.system_interface.template_ref import read_template_ref
from imbue.system_interface.template_ref import resolve_template_ref

_MARKER = "update-self: merge upstream template ({})"


@pytest.mark.parametrize(
    ("marker_subject", "described_tag", "expected"),
    [
        ("", "", ""),
        ("", "minds-v0.9.0\n", "minds-v0.9.0"),
        (_MARKER.format("minds-v0.10.2"), "", "minds-v0.10.2"),
        # A workspace updated to main still describes the release it was created at.
        (_MARKER.format("main"), "minds-v0.9.0", "main"),
        (_MARKER.format("minds-v0.10.2"), "minds-v0.9.0", "minds-v0.10.2"),
        # A run landed with an ordinary merge leaves its marker off the first-parent line.
        (_MARKER.format("minds-v0.9.0"), "minds-v0.10.2", "minds-v0.10.2"),
        (_MARKER.format("minds-v0.10.0-rc.1"), "minds-v0.10.0", "minds-v0.10.0"),
        (_MARKER.format("minds-v0.10.0"), "minds-v0.10.0-rc.2", "minds-v0.10.0"),
        (_MARKER.format("minds-v0.10.0-rc.2"), "minds-v0.10.0-rc.10", "minds-v0.10.0-rc.10"),
        # A subject the grep selected but that is not the marker names nothing.
        ("update-self: merge upstream template (broken", "minds-v0.9.0", "minds-v0.9.0"),
    ],
)
def test_the_merge_marker_outranks_the_tag_unless_the_tag_is_a_newer_release(
    marker_subject: str, described_tag: str, expected: str
) -> None:
    assert resolve_template_ref(marker_subject, described_tag) == expected


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
