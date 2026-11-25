from pathlib import Path

import pytest

from workspace_update_control.installed_version import (
    is_release_behind,
    read_installed_ref,
    resolve_template_ref,
)
from workspace_update_control.testing import stage_checkout_at

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
        (
            _MARKER.format("minds-v0.10.0-rc.2"),
            "minds-v0.10.0-rc.10",
            "minds-v0.10.0-rc.10",
        ),
        # A subject the grep selected but that is not the marker names nothing.
        (
            "update-self: merge upstream template (broken",
            "minds-v0.9.0",
            "minds-v0.9.0",
        ),
    ],
)
def test_the_merge_marker_outranks_the_tag_unless_the_tag_is_a_newer_release(
    marker_subject: str, described_tag: str, expected: str
) -> None:
    assert resolve_template_ref(marker_subject, described_tag) == expected


@pytest.mark.parametrize(
    ("installed_ref", "target_ref", "is_behind"),
    [
        ("minds-v0.9.0", "minds-v0.10.0", True),
        # A patch is a release like any other.
        ("minds-v0.10.5", "minds-v0.10.6", True),
        ("minds-v0.10.0-rc.1", "minds-v0.10.0", True),
        ("minds-v0.10.0", "minds-v0.10.0", False),
        # A workspace ahead of the target has nothing to update to.
        ("minds-v0.11.0", "minds-v0.10.0", False),
        # A checkout that names no release is not a reading that it is behind.
        ("", "minds-v0.10.0", False),
        ("main", "minds-v0.10.0", False),
        ("minds-v0.9.0", "main", False),
    ],
)
def test_a_workspace_is_behind_only_a_release_that_sorts_above_its_own(
    installed_ref: str, target_ref: str, is_behind: bool
) -> None:
    assert is_release_behind(installed_ref, target_ref) is is_behind


def test_a_checkout_is_read_by_its_tag_and_then_by_its_update_self_merge(
    tmp_path: Path,
) -> None:
    stage_checkout_at(tmp_path, ("the template at a release", "minds-v0.9.0"))

    assert read_installed_ref(str(tmp_path)) == "minds-v0.9.0"

    stage_checkout_at(tmp_path, (_MARKER.format("minds-v0.10.2"), ""))

    assert read_installed_ref(str(tmp_path)) == "minds-v0.10.2"


def test_a_directory_that_is_no_checkout_names_no_version(tmp_path: Path) -> None:
    assert read_installed_ref(str(tmp_path)) == ""
