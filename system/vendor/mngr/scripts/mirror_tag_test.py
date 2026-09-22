import time
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from scripts.mirror_tag import ExportedAncestor
from scripts.mirror_tag import GitCommandError
from scripts.mirror_tag import MirrorNotCaughtUpError
from scripts.mirror_tag import NoPublicCommitError
from scripts.mirror_tag import PublicTagConflictError
from scripts.mirror_tag import PublicTipNotExportedError
from scripts.mirror_tag import TagNotOnMirroredBranchError
from scripts.mirror_tag import _run_git_process
from scripts.mirror_tag import find_exported_ancestor
from scripts.mirror_tag import landing_commit_index
from scripts.mirror_tag import main
from scripts.mirror_tag import plan_public_tag
from scripts.mirror_tag import push_public_tag
from scripts.mirror_tag import read_tag_message
from scripts.mirror_tag import run_git
from scripts.mirror_tag import tags_missing_from_mirror
from scripts.mirror_tag import wait_for_public_tag_plan

pytestmark = pytest.mark.usefixtures("isolated_git")


def _git(repo: Path, *args: str) -> str:
    return run_git_command(repo, *args).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    (repo / f"{uuid4().hex}.txt").write_text(message)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _commit_exported(public_repo: Path, private_sha: str) -> str:
    return _commit(public_repo, f"export of {private_sha[:8]}\n\nGitOrigin-RevId: {private_sha}")


def _bare_clone(source_url: str, destination: Path) -> Path:
    run_git_command(destination.parent, "clone", "-q", "--bare", source_url, str(destination))
    return destination


class _MirrorPair:
    """A private repo with a linear main and a public repo exporting a subset of it."""

    def __init__(self, tmp_path: Path) -> None:
        self.private = tmp_path / "private"
        self.public_work = tmp_path / "public-work"
        self.public_bare = tmp_path / "public.git"
        init_git_repo(self.private, initial_commit=False)
        init_git_repo(self.public_work, initial_commit=False)
        self.public_sha_by_private_sha: dict[str, str] = {}

    def add_private_commit(self, message: str, is_exported: bool) -> str:
        private_sha = _commit(self.private, message)
        if is_exported:
            self.public_sha_by_private_sha[private_sha] = _commit_exported(self.public_work, private_sha)
        return private_sha

    def tag_private(self, tag_name: str, sha: str, message: str) -> None:
        _git(self.private, "tag", "-a", tag_name, sha, "-m", message)

    def merge_private_branch(self, branch: str, is_exported: bool) -> str:
        """Land ``branch`` on the checked-out private branch with a ``--no-ff`` merge, the way a release lands on main."""
        _git(self.private, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        merge_sha = _git(self.private, "rev-parse", "HEAD")
        if is_exported:
            self.public_sha_by_private_sha[merge_sha] = _commit_exported(self.public_work, merge_sha)
        return merge_sha

    def publish(self) -> None:
        """Materialize the bare public repo the script clones from and pushes to."""
        _bare_clone(self.public_work.as_uri(), self.public_bare)

    def publish_and_clone(self) -> Path:
        """Publish, then return the script-side clone of the mirror (what main() makes in its temp dir)."""
        self.publish()
        return _bare_clone(self.public_url(), self.public_bare.parent / "clone.git")

    def public_url(self) -> str:
        return self.public_bare.as_uri()

    def tag_off_main(self, tag_name: str, message: str) -> str:
        """Tag a commit on a side branch that main never merges, so no exported commit carries its tree."""
        branch = f"experiment-{uuid4().hex[:8]}"
        _git(self.private, "checkout", "-q", "-b", branch)
        off_main_sha = _commit(self.private, branch)
        _git(self.private, "checkout", "-q", "main")
        self.tag_private(tag_name, off_main_sha, message)
        return off_main_sha


def _main_args(pair: _MirrorPair, *extra: str) -> list[str]:
    """The argv every ``main()`` test shares: the pair's repos, main as the mirrored ref, and no catch-up wait."""
    return [
        "--private-repo",
        str(pair.private),
        "--public-url",
        pair.public_url(),
        "--mirrored-ref",
        "main",
        "--catch-up-timeout-seconds",
        "0",
        *extra,
    ]


def test_find_exported_ancestor_prefers_the_newest_exported_commit() -> None:
    history = ["c3", "c2", "c1"]
    mapping = {"c2": "p2", "c1": "p1"}

    assert find_exported_ancestor(history, mapping) == ExportedAncestor(
        private_sha="c2", public_sha="p2", walked_commit_count=2
    )


def test_find_exported_ancestor_returns_none_when_nothing_was_exported() -> None:
    assert find_exported_ancestor(["c3", "c2"], {"other": "p"}) is None


def test_find_exported_ancestor_handles_empty_history() -> None:
    assert find_exported_ancestor([], {"c1": "p1"}) is None


@pytest.mark.parametrize(
    ("containing_shas", "expected_index"),
    [
        # The tag sits on the chain: it is its own landing commit.
        ({"m5", "m4", "m3"}, 2),
        # The tag is off the chain and m2 is the merge that landed it.
        ({"m5", "m4", "m3", "m2"}, 3),
        # Only the tip contains the tag.
        ({"m5"}, 0),
        # Every chain commit contains the tag (it predates the root).
        ({"m5", "m4", "m3", "m2", "m1"}, 4),
    ],
)
def test_landing_commit_index_finds_the_oldest_chain_commit_containing_the_tag(
    containing_shas: set[str], expected_index: int
) -> None:
    chain = ["m5", "m4", "m3", "m2", "m1"]

    assert landing_commit_index(chain, lambda sha: sha in containing_shas) == expected_index


def test_tags_missing_from_mirror_filters_by_glob_and_sorts_by_version() -> None:
    private_tags = [
        "minds-v10.0.0",
        "minds-v0.10.0",
        "minds-v0.9.1-rc1",
        "minds-v0.9.1",
        "v0.2.17",
        "minds-v0.9.0",
        "minds-v0.9.1-1",
    ]

    missing = tags_missing_from_mirror(private_tags, ["minds-v0.9.0"], "minds-v*")

    assert missing == ["minds-v0.9.1", "minds-v0.9.1-1", "minds-v0.9.1-rc1", "minds-v0.10.0", "minds-v10.0.0"]


def test_plan_maps_a_tag_on_an_exported_commit_directly(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    tagged = pair.add_private_commit("release", is_exported=True)
    pair.tag_private("minds-v1.0.0", tagged, "minds 1.0.0")
    public_clone = pair.publish_and_clone()

    plan = plan_public_tag(pair.private, public_clone, "minds-v1.0.0", "main")

    assert plan.public_sha == pair.public_sha_by_private_sha[tagged]
    assert plan.landing_private_sha == tagged
    assert plan.exported_private_sha == tagged
    assert plan.walked_commit_count == 1
    assert plan.message == "minds 1.0.0"
    assert plan.existing_public_sha is None


def test_plan_maps_a_tag_on_a_merged_release_branch_to_the_merge_that_landed_it(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    _git(pair.private, "checkout", "-q", "-b", "release")
    # Copybara never sees release-branch commits: only main's first-parent chain is exported.
    tagged = pair.add_private_commit("version bump", is_exported=False)
    pair.add_private_commit("post-tag fix on the release branch", is_exported=False)
    _git(pair.private, "checkout", "-q", "main")
    pair.add_private_commit("unrelated change that moved main", is_exported=True)
    merge = pair.merge_private_branch("release", is_exported=True)
    pair.tag_private("minds-v1.5.0", tagged, "minds 1.5.0")
    public_clone = pair.publish_and_clone()

    plan = plan_public_tag(pair.private, public_clone, "minds-v1.5.0", "main")

    # Neither the branch point (whose tree predates the bump) nor the release
    # branch itself: the merge is the first exported commit carrying the bump.
    assert plan.public_sha == pair.public_sha_by_private_sha[merge]
    assert plan.landing_private_sha == merge
    assert plan.exported_private_sha == merge
    assert plan.walked_commit_count == 1


def test_plan_walks_first_parent_ancestors_past_private_only_commits(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    exported = pair.add_private_commit("public change", is_exported=True)
    pair.add_private_commit("private-only change", is_exported=False)
    tagged = pair.add_private_commit("another private-only change", is_exported=False)
    # The mirror is caught up past the tag: a later exported commit exists.
    pair.add_private_commit("later public change", is_exported=True)
    pair.tag_private("minds-v1.1.0", tagged, "minds 1.1.0")
    public_clone = pair.publish_and_clone()

    plan = plan_public_tag(pair.private, public_clone, "minds-v1.1.0", "main")

    assert plan.public_sha == pair.public_sha_by_private_sha[exported]
    assert plan.exported_private_sha == exported
    assert plan.walked_commit_count == 3


def test_plan_raises_when_the_mirror_has_not_exported_the_tagged_commit_yet(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    tagged = pair.add_private_commit("not yet mirrored", is_exported=False)
    pair.tag_private("minds-v1.2.0", tagged, "minds 1.2.0")
    public_clone = pair.publish_and_clone()

    with pytest.raises(MirrorNotCaughtUpError, match="not yet exported"):
        plan_public_tag(pair.private, public_clone, "minds-v1.2.0", "main")


def test_plan_treats_an_export_the_checkout_has_not_fetched_as_the_mirror_being_caught_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    tagged = pair.add_private_commit("release", is_exported=True)
    pair.tag_private("minds-v1.5.0", tagged, "minds 1.5.0")
    # The workflow's checkout is taken once; main keeps moving and the mirror
    # exports a commit the checkout never fetched.
    stale_checkout = tmp_path / "checkout"
    run_git_command(tmp_path, "clone", "-q", str(pair.private), str(stale_checkout))
    later = pair.add_private_commit("landed after the checkout", is_exported=True)
    public_clone = pair.publish_and_clone()

    plan = plan_public_tag(stale_checkout, public_clone, "minds-v1.5.0", "main")

    assert plan.public_sha == pair.public_sha_by_private_sha[tagged]
    assert f"newest exported commit ({later[:12]}) is not in the private checkout" in capsys.readouterr().out


def test_plan_raises_when_no_ancestor_of_the_landing_commit_was_ever_exported(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=False)
    tagged = pair.add_private_commit("release before the first export", is_exported=False)
    pair.tag_private("minds-v0.9.0", tagged, "minds 0.9.0")
    # The mirror is past the tag, so this is not a lagging mirror: nothing at
    # or before the tagged commit ever produced a public commit.
    pair.add_private_commit("first exported change", is_exported=True)
    public_clone = pair.publish_and_clone()

    with pytest.raises(NoPublicCommitError, match=f"no first-parent ancestor of {tagged[:12]}.*was ever exported"):
        plan_public_tag(pair.private, public_clone, "minds-v0.9.0", "main")


def test_plan_raises_immediately_when_the_public_tip_was_not_produced_by_the_sync(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    tagged = pair.add_private_commit("release", is_exported=True)
    pair.tag_private("minds-v1.4.0", tagged, "minds 1.4.0")
    # Someone pushed straight to the public branch, past the last exported commit.
    manual = _commit(pair.public_work, "manual public commit without a trailer")
    public_clone = pair.publish_and_clone()

    with pytest.raises(PublicTipNotExportedError, match=f"{manual[:12]}.*no GitOrigin-RevId trailer"):
        wait_for_public_tag_plan(
            pair.private,
            public_clone,
            pair.public_url(),
            "minds-v1.4.0",
            "main",
            deadline=time.monotonic() + 30,
            poll_interval_seconds=0.01,
        )


def test_wait_gives_up_at_the_deadline_and_retries_after_the_mirror_catches_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = _MirrorPair(tmp_path)
    seed = pair.add_private_commit("seed", is_exported=True)
    tagged = pair.add_private_commit("release", is_exported=False)
    pair.tag_private("minds-v1.3.0", tagged, "minds 1.3.0")
    # The bare mirror the script clones from is stale: it predates the export
    # that moves the mirror past the tagged commit.
    public_clone = pair.publish_and_clone()
    pair.add_private_commit("later public change", is_exported=True)
    fresh_mirror = _bare_clone(pair.public_work.as_uri(), tmp_path / "fresh.git")

    with pytest.raises(MirrorNotCaughtUpError):
        wait_for_public_tag_plan(
            pair.private,
            public_clone,
            fresh_mirror.as_uri(),
            "minds-v1.3.0",
            "main",
            deadline=time.monotonic(),
            poll_interval_seconds=0.01,
        )
    assert "retrying" not in capsys.readouterr().out

    plan = wait_for_public_tag_plan(
        pair.private,
        public_clone,
        fresh_mirror.as_uri(),
        "minds-v1.3.0",
        "main",
        deadline=time.monotonic() + 30,
        poll_interval_seconds=0.01,
    )

    assert "minds-v1.3.0: mirror not caught up yet; retrying" in capsys.readouterr().out
    assert plan.public_sha == pair.public_sha_by_private_sha[seed]
    assert plan.walked_commit_count == 2


def test_plan_raises_for_a_tag_that_is_not_on_the_mirrored_branch(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    _git(pair.private, "checkout", "-q", "-b", "test-branch")
    branch_only = _commit(pair.private, "branch-only")
    _git(pair.private, "checkout", "-q", "main")
    pair.tag_private("minds-v9.9.9", branch_only, "test tag")
    public_clone = pair.publish_and_clone()

    with pytest.raises(TagNotOnMirroredBranchError, match="not reachable from main"):
        plan_public_tag(pair.private, public_clone, "minds-v9.9.9", "main")


def test_git_errors_redact_url_credentials(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=False)

    with pytest.raises(GitCommandError) as excinfo:
        run_git(repo, "no-such-subcommand", "https://x-access-token:hunter2@github.com/imbue-ai/mngr.git")

    assert "hunter2" not in str(excinfo.value)
    assert "https://***@github.com/imbue-ai/mngr.git" in str(excinfo.value)


def test_read_tag_message_uses_the_name_for_a_lightweight_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=False)
    sha = _commit(repo, "seed")
    _git(repo, "tag", "light", sha)

    assert read_tag_message(repo, "light") == "light"


def test_read_tag_message_drops_the_signature_of_a_signed_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=False)
    sha = _commit(repo, "seed")
    # A signed tag stores its signature block at the end of the message payload.
    message_file = tmp_path / "tag-message.txt"
    message_file.write_text(
        "minds 1.0.0: verified pair\n\nbody line\n-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n"
    )
    _git(repo, "tag", "-a", "-F", str(message_file), "signed", sha)
    assert _git(repo, "tag", "-l", "--format=%(contents:signature)", "signed") != ""

    assert read_tag_message(repo, "signed") == "minds 1.0.0: verified pair\n\nbody line"


def test_push_creates_the_annotated_tag_on_the_mirror_and_a_rerun_is_a_no_op(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    pair.add_private_commit("seed", is_exported=True)
    tagged = pair.add_private_commit("release", is_exported=True)
    pair.tag_private("minds-v2.0.0", tagged, "minds 2.0.0: verified pair")
    public_clone = pair.publish_and_clone()
    plan = plan_public_tag(pair.private, public_clone, "minds-v2.0.0", "main")

    push_public_tag(public_clone, plan, pair.public_url(), is_force=False)

    assert _git(pair.public_bare, "rev-parse", "minds-v2.0.0^{commit}") == pair.public_sha_by_private_sha[tagged]
    assert _git(pair.public_bare, "cat-file", "-t", "minds-v2.0.0") == "tag"
    assert _git(pair.public_bare, "tag", "-l", "--format=%(contents:subject)", "minds-v2.0.0") == (
        "minds 2.0.0: verified pair"
    )
    rerun_clone = _bare_clone(pair.public_url(), tmp_path / "rerun.git")
    rerun_plan = plan_public_tag(pair.private, rerun_clone, "minds-v2.0.0", "main")
    assert rerun_plan.is_already_in_place


def test_push_refuses_to_move_an_existing_public_tag_without_force(tmp_path: Path) -> None:
    pair = _MirrorPair(tmp_path)
    first = pair.add_private_commit("first", is_exported=True)
    second = pair.add_private_commit("second", is_exported=True)
    pair.tag_private("minds-v3.0.0", second, "minds 3.0.0")
    # The mirror already carries the tag on the wrong (older) commit.
    _git(pair.public_work, "tag", "-a", "minds-v3.0.0", pair.public_sha_by_private_sha[first], "-m", "stale")
    public_clone = pair.publish_and_clone()
    plan = plan_public_tag(pair.private, public_clone, "minds-v3.0.0", "main")
    assert plan.existing_public_sha == pair.public_sha_by_private_sha[first]

    with pytest.raises(PublicTagConflictError, match="pass --force"):
        push_public_tag(public_clone, plan, pair.public_url(), is_force=False)

    push_public_tag(public_clone, plan, pair.public_url(), is_force=True)
    assert _git(pair.public_bare, "rev-parse", "minds-v3.0.0^{commit}") == pair.public_sha_by_private_sha[second]


def test_a_git_command_slower_than_expected_is_reported_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=False)

    assert _run_git_process(repo, "status", slow_warning_seconds=0.0).returncode == 0
    assert "warning: git status took" in capsys.readouterr().err

    assert run_git(repo, "status") != ""
    assert capsys.readouterr().err == ""


def test_main_all_missing_pushes_mirrorable_tags_and_skips_branch_only_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = _MirrorPair(tmp_path)
    older = pair.add_private_commit("older release", is_exported=True)
    pair.tag_private("minds-v0.1.0", older, "minds 0.1.0")
    newer = pair.add_private_commit("newer release", is_exported=True)
    pair.tag_private("minds-v0.2.0", newer, "minds 0.2.0")
    pair.tag_off_main("minds-v0.3.0", "test tag off main")
    _git(pair.private, "tag", "-a", "v0.9.9", older, "-m", "not a minds tag")
    # The mirror already has the oldest tag in the right place.
    _git(pair.public_work, "tag", "-a", "minds-v0.1.0", pair.public_sha_by_private_sha[older], "-m", "minds 0.1.0")
    pair.publish()

    exit_code = main(_main_args(pair, "--all-missing", "--push-url", pair.public_url()))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "2 minds-v* tag(s) missing from the mirror: minds-v0.2.0, minds-v0.3.0" in output
    assert "minds-v0.3.0: skipped" in output
    assert _git(pair.public_bare, "tag", "-l", "minds-v*").splitlines() == ["minds-v0.1.0", "minds-v0.2.0"]
    assert _git(pair.public_bare, "rev-parse", "minds-v0.2.0^{commit}") == pair.public_sha_by_private_sha[newer]


def test_main_dry_run_pushes_nothing_and_an_explicit_off_branch_tag_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = _MirrorPair(tmp_path)
    tagged = pair.add_private_commit("release", is_exported=True)
    pair.tag_private("minds-v0.5.0", tagged, "minds 0.5.0")
    pair.tag_off_main("minds-v0.6.0", "test tag off main")
    pair.publish()

    exit_code = main(_main_args(pair, "--tag", "minds-v0.5.0", "--tag", "minds-v0.6.0", "--dry-run"))

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "minds-v0.5.0: private" in captured.out
    assert "new on the mirror" in captured.out
    assert "minds-v0.6.0: error" in captured.err
    assert _git(pair.public_bare, "tag", "-l", "minds-v*") == ""
