from pathlib import Path
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_mapreduce.archive import ARCHIVE_FILENAME
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBDIR
from imbue.mngr_mapreduce.bundle import BRANCH_BUNDLE_NAME
from imbue.mngr_mapreduce.bundle import PUBLISH_OUTPUTS_SNIPPET
from imbue.mngr_mapreduce.bundle import apply_branch_bundle
from imbue.mngr_mapreduce.bundle import has_local_branch


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def _clone_then_bundle_a_new_branch(cg: ConcurrencyGroup, origin: Path, clone: Path, branch_name: str) -> Path:
    """Clone the repo at its current tip, then commit on a new branch in the origin and bundle that branch."""
    cg.run_process_to_completion(["git", "clone", "--quiet", str(origin), str(clone)])
    base_commit = _git(cg, origin, "rev-parse", "HEAD")
    _git(cg, origin, "checkout", "--quiet", "-b", branch_name)
    (origin / f"{uuid4().hex}.txt").write_text("published by an agent\n")
    _git(cg, origin, "add", ".")
    _git(cg, origin, "commit", "--quiet", "-m", "agent work")
    bundle_path = origin.parent / BRANCH_BUNDLE_NAME
    _git(cg, origin, "bundle", "create", str(bundle_path), f"{base_commit}..{branch_name}")
    return bundle_path


def test_apply_branch_bundle_fetches_the_branch_into_a_repo_that_lacks_it(
    temp_git_repo: Path, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    clone = tmp_path / "clone"
    branch_name = f"agents/{uuid4().hex}"
    bundle_path = _clone_then_bundle_a_new_branch(cg, temp_git_repo, clone, branch_name)
    assert not has_local_branch(clone, branch_name, cg)

    is_applied = apply_branch_bundle(clone, bundle_path, branch_name, "agent-under-test", cg)

    assert is_applied
    assert has_local_branch(clone, branch_name, cg)
    assert _git(cg, clone, "rev-parse", branch_name) == _git(cg, temp_git_repo, "rev-parse", branch_name)


def test_apply_branch_bundle_reports_failure_for_a_missing_bundle(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    missing_bundle = temp_git_repo.parent / "no-such.bundle"

    is_applied = apply_branch_bundle(temp_git_repo, missing_bundle, "never-created", "agent-under-test", cg)

    assert not is_applied
    assert not has_local_branch(temp_git_repo, "never-created", cg)


def test_publish_snippet_writes_the_archive_and_bundle_where_the_framework_looks() -> None:
    assert f'"$MNGR_AGENT_STATE_DIR/{ARCHIVE_SUBDIR}"' in PUBLISH_OUTPUTS_SNIPPET
    assert f'"$ARCHIVE_DIR/{ARCHIVE_FILENAME}"' in PUBLISH_OUTPUTS_SNIPPET
    assert f'"$STAGING/{BRANCH_BUNDLE_NAME}"' in PUBLISH_OUTPUTS_SNIPPET
    assert "$MNGR_GIT_BASE_BRANCH..$BRANCH" in PUBLISH_OUTPUTS_SNIPPET
