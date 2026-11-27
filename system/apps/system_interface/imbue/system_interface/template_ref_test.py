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


def test_a_directory_that_is_no_checkout_names_no_version_and_the_log_says_why(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    assert read_template_ref(tmp_path) == ""

    # The marker's read never exits with an error in a checkout, so one is a fault worth a warning.
    warnings = [record for record in loguru_records if record.startswith("WARNING")]
    assert len(warnings) == 1
    assert "`git log" in warnings[0] and str(tmp_path) in warnings[0]


def test_a_checkout_with_no_tag_to_describe_is_not_a_fault(tmp_path: Path, loguru_records: list[str]) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "commit", "--allow-empty", "--quiet", "-m", "a workspace made from a published template")

    assert read_template_ref(tmp_path) == ""

    assert [record for record in loguru_records if not record.startswith("DEBUG")] == []
    assert any("`git describe" in record for record in loguru_records)
