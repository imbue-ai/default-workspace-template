import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_install.cli import main
from app_install.errors import LockCheckError
from app_install.errors import NotAssembledError
from app_install.errors import NotModifiedAppError
from app_install.errors import NotPushedError
from app_install.errors import PublishedNameTakenError
from app_install.errors import PublishError
from app_install.errors import PushRefusedError
from app_install.errors import SecretsFoundError
from app_install.git import is_commit_present
from app_install.git import run_git
from app_install.installing import install_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import add_app
from app_install.publishing import ChangedPath
from app_install.publishing import ChangeKind
from app_install.publishing import PushOutcome
from app_install.publishing import assemble_app
from app_install.publishing import compare_with_published
from app_install.publishing import pending_publish_path
from app_install.publishing import push_app
from app_install.publishing import record_publish
from app_install.publishing import set_version
from app_install.runtime import SECRET_SCAN_SCRIPT
from app_install.runtime import WorkspaceRuntime
from app_install.testing import commit_all
from app_install.testing import make_bare_repository
from app_install.testing import make_published_app_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import point_url_at
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_manifest.records import load_record

_PUBLISHED_URL = "https://github.com/octo/notes"
_TEMPLATE_VERSION = "minds-v0.11.0"
_EXCLUDES = '\n[publish]\nexclude = ["docs/internal/**", "*.draft.md"]\n'


def _publisher(
    tmp_path: Path, extra_manifest: str = "", is_python: bool = True
) -> tuple[Path, Path, RecordingRuntime]:
    """A workspace on a template release that built the notes app, the bare repository standing in for the one
    its publish creates on GitHub, and a runtime that really commits."""
    workspace = make_workspace_repository(tmp_path / "publisher")
    run_git(workspace, ["tag", _TEMPLATE_VERSION])
    package = write_user_app(workspace, "notes", extra_manifest, is_python=is_python)
    write_app_file(workspace, package, "src/notes/runner.py", "GREETING = 'hello'\n")
    add_app(workspace, package, None, never_bound)
    commit_all(workspace, "Add the notes app")
    published = make_bare_repository(tmp_path / "github" / "notes.git")
    point_url_at(workspace, _PUBLISHED_URL, published)
    return workspace, published, RecordingRuntime(repo_root=workspace, is_committing=True)


def _versioned(workspace: Path, runtime: RecordingRuntime, version: str) -> str:
    """Set the app's version and commit it, as a publish's worker does; the commit."""
    set_version(workspace, "notes", version, runtime)
    return commit_all(workspace, f"notes {version}")


def _publish(workspace: Path, runtime: RecordingRuntime, version: str) -> PushOutcome:
    _versioned(workspace, runtime, version)
    assemble_app(workspace, "notes", "HEAD", runtime)
    pushed = push_app(workspace, "notes", None, runtime)
    record_publish(workspace, "notes", runtime)
    return pushed


def _published_files(published: Path, revision: str = "main") -> list[str]:
    return run_git(published, ["ls-tree", "-r", "--name-only", revision]).splitlines()


def _refs(repository: Path) -> list[str]:
    return run_git(repository, ["for-each-ref", "--format=%(refname)"]).splitlines()


def test_setting_the_version_writes_it_to_the_manifest_the_project_and_the_lock(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path)
    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    manifest_file.write_text(manifest_file.read_text() + '# How the app starts.\n[run]\ncommand = "notes"\n')

    outcome = set_version(workspace, "notes", "0.1.0", runtime)

    assert outcome.version == "0.1.0"
    assert outcome.previous_version == ""
    assert outcome.verified_template == _TEMPLATE_VERSION
    assert outcome.problems == ()
    assert outcome.changed_paths == (
        "system/apps/notes/app.toml",
        "system/apps/notes/pyproject.toml",
        "system/apps/notes/uv.lock",
    )
    manifest_text = manifest_file.read_text()
    # The new keys are top-level keys, written before the first table, and the file's comments are kept.
    assert manifest_text.index('version = "0.1.0"') < manifest_text.index("[run]")
    assert manifest_text.index(f'verified_template = "{_TEMPLATE_VERSION}"') < manifest_text.index("[run]")
    assert "# How the app starts." in manifest_text
    assert 'version = "0.1.0"' in (workspace / "system" / "apps" / "notes" / "pyproject.toml").read_text()
    assert "relock_app notes" in runtime.calls
    # Nothing is committed: the publish's worker commits its own work.
    assert run_git(workspace, ["status", "--porcelain"]) != ""

    later = set_version(workspace, "notes", "0.2.0", runtime)
    assert later.previous_version == "0.1.0"


def test_a_version_that_is_malformed_or_not_newer_than_the_published_one_is_refused(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    _publish(workspace, runtime, "1.2.0")
    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    before = manifest_file.read_text()

    for version in ("1.2.0", "1.1.9", "1.2.0-rc1", "two", "1.2"):
        with pytest.raises(PublishError):
            set_version(workspace, "notes", version, runtime)

    assert manifest_file.read_text() == before
    assert set_version(workspace, "notes", "2.0.0-rc1", runtime).version == "2.0.0-rc1"


def test_a_workspace_with_no_template_release_in_its_history_says_so(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    run_git(workspace, ["tag", "-d", _TEMPLATE_VERSION])

    outcome = set_version(workspace, "notes", "0.1.0", runtime)

    assert outcome.verified_template == ""
    assert outcome.problems == (
        "the workspace's history holds no template release tag, so verified_template was left as it was",
    )
    assert "verified_template" not in (workspace / "system" / "apps" / "notes" / "app.toml").read_text()


def test_the_published_tree_leaves_out_what_the_manifest_excludes(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, _EXCLUDES)
    write_app_file(workspace, "notes", "docs/internal/roadmap.md", "private plans\n")
    write_app_file(workspace, "notes", "docs/guide.md", "how to use it\n")
    write_app_file(workspace, "notes", "notes.draft.md", "half written\n")
    write_app_file(workspace, "notes", "src/notes/ideas.draft.md", "half written\n")
    source_commit = _versioned(workspace, runtime, "0.1.0")

    assembled = assemble_app(workspace, "notes", "HEAD", runtime)

    kept = [
        "app.toml",
        "docs/guide.md",
        "icon.svg",
        "pyproject.toml",
        "src/notes/runner.py",
        "uv.lock",
    ]
    assert sorted(assembled.published.paths) == kept
    assert sorted(assembled.published.excluded_paths) == [
        "docs/internal/roadmap.md",
        "notes.draft.md",
        "src/notes/ideas.draft.md",
    ]
    assert assembled.published.source_commit == source_commit
    # The scan ran over exactly the tree that will be pushed.
    assert list(runtime.scanned_file_names[-1]) == kept
    # Nothing is pushed by assembling.
    assert _refs(published) == []

    push_app(workspace, "notes", None, runtime)

    # The app's directory is the repository's root, and the excluded files never left the machine.
    assert _published_files(published) == kept
    assert run_git(published, ["rev-parse", "main^{tree}"]) == assembled.published.tree
    # The workspace's own copy still has them.
    assert (workspace / "system" / "apps" / "notes" / "docs" / "internal" / "roadmap.md").is_file()


def test_a_planted_secret_stops_the_publish_and_nothing_is_pushed(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, _EXCLUDES)
    runtime.secret_marker = "PLANTED-SECRET"
    write_app_file(workspace, "notes", "src/notes/settings.py", "API_KEY = 'PLANTED-SECRET'\n")
    _versioned(workspace, runtime, "0.1.0")

    with pytest.raises(SecretsFoundError) as stopped:
        assemble_app(workspace, "notes", "HEAD", runtime)

    assert stopped.value.findings == ("[stand-in] planted-secret: src/notes/settings.py (value redacted)",)
    assert not pending_publish_path(workspace, "notes").exists()
    with pytest.raises(NotAssembledError, match="publish assemble notes"):
        push_app(workspace, "notes", None, runtime)
    assert _refs(published) == []
    assert not any(call.startswith("create_published_repository") for call in runtime.calls)

    # Excluding the file is one of the two ways on: what is left out is neither scanned nor published.
    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    manifest_file.write_text(
        manifest_file.read_text().replace('"*.draft.md"', '"*.draft.md", "src/notes/settings.py"')
    )
    commit_all(workspace, "Leave the settings out of what is published")
    assembled = assemble_app(workspace, "notes", "HEAD", runtime)
    assert "src/notes/settings.py" in assembled.published.excluded_paths
    push_app(workspace, "notes", None, runtime)
    assert "src/notes/settings.py" not in _published_files(published)


def test_a_scan_that_cannot_run_stops_the_publish(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    runtime.scan_failure = "SCANNER MISSING: 'kingfisher' is not installed"
    _versioned(workspace, runtime, "0.1.0")

    with pytest.raises(PublishError, match="did not run to the end"):
        assemble_app(workspace, "notes", "HEAD", runtime)

    assert not pending_publish_path(workspace, "notes").exists()
    assert _refs(published) == []


def test_the_lock_is_checked_in_the_assembled_tree_outside_the_workspace(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path)
    _versioned(workspace, runtime, "0.1.0")

    assembled = assemble_app(workspace, "notes", "HEAD", runtime)

    assert assembled.is_lock_checked
    (checked,) = runtime.lock_checked_directories
    assert not checked.resolve().is_relative_to(workspace.resolve())
    assert workspace.resolve() not in checked.resolve().parents
    # The directory held the app alone, with its project at the root, and is gone again.
    assert "pyproject.toml" in runtime.scanned_file_names[-1]
    assert not any(name.startswith("system/") for name in runtime.scanned_file_names[-1])
    assert not checked.exists()

    runtime.lock_failure = "error: The lockfile at `uv.lock` needs to be updated"
    with pytest.raises(LockCheckError, match="needs to be updated"):
        assemble_app(workspace, "notes", "HEAD", runtime)
    assert not pending_publish_path(workspace, "notes").exists()


def test_an_app_with_no_python_project_has_no_lock_to_check(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    _versioned(workspace, runtime, "0.1.0")

    assembled = assemble_app(workspace, "notes", "HEAD", runtime)

    assert not assembled.is_lock_checked
    assert "check_lock" not in runtime.calls


def test_the_real_lock_check_passes_a_current_lock_and_fails_a_stale_one(tmp_path: Path) -> None:
    project = tmp_path / "standalone"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "notes"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    )
    runtime = WorkspaceRuntime(repo_root=tmp_path)
    subprocess.run(["uv", "lock", "--offline"], cwd=project, check=True, capture_output=True)

    assert runtime.check_lock(project) is None

    (project / "pyproject.toml").write_text(
        '[project]\nname = "notes"\nversion = "0.2.0"\nrequires-python = ">=3.12"\n'
    )
    failure = runtime.check_lock(project)
    assert failure is not None and "uv.lock" in failure


def test_an_app_must_name_a_version_and_a_commit_that_holds_it(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)

    with pytest.raises(PublishError, match="names no version"):
        assemble_app(workspace, "notes", "HEAD", runtime)
    with pytest.raises(PublishError, match="not a commit that holds system/apps/notes"):
        assemble_app(workspace, "notes", "HEAD~1", runtime)
    with pytest.raises(PublishError, match="not a commit"):
        assemble_app(workspace, "notes", "no-such-branch", runtime)


def test_a_tree_that_excludes_its_own_manifest_or_icon_is_not_published(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, '\n[publish]\nexclude = ["icon.svg"]\n', is_python=False)
    _versioned(workspace, runtime, "0.1.0")

    with pytest.raises(PublishError, match="icon"):
        assemble_app(workspace, "notes", "HEAD", runtime)

    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    manifest_file.write_text(manifest_file.read_text().replace('"icon.svg"]', '"app.toml"]'))
    commit_all(workspace, "Exclude the manifest")
    with pytest.raises(PublishError, match="not an app"):
        assemble_app(workspace, "notes", "HEAD", runtime)


def test_a_first_publish_creates_the_repository_and_pushes_one_parentless_commit_and_its_tag(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)

    pushed = push_app(workspace, "notes", None, runtime)

    assert pushed.repo_url == _PUBLISHED_URL
    assert pushed.is_repository_created
    assert pushed.is_tag_pushed
    assert pushed.parent == ""
    assert pushed.tag == "v0.1.0"
    assert pushed.derived_from == ""
    assert pushed.problems == ()
    assert "create_published_repository notes 'Notes'" in runtime.calls
    assert sorted(_refs(published)) == ["refs/heads/main", "refs/tags/v0.1.0"]
    assert run_git(published, ["rev-parse", "main"]) == pushed.commit
    assert run_git(published, ["rev-parse", "v0.1.0^{commit}"]) == pushed.commit
    assert run_git(published, ["log", "--format=%P|%an|%B", "main"]) == "|app-install|notes v0.1.0"
    # The workspace keeps the published commit for the next publish to follow, and no tag of the app's.
    assert run_git(workspace, ["rev-parse", "refs/app-published/notes"]) == pushed.commit
    assert run_git(workspace, ["tag", "--list"]) == _TEMPLATE_VERSION


def test_a_second_publish_follows_the_first_and_is_pushed_as_a_fast_forward(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    first = _publish(workspace, runtime, "0.1.0")
    write_app_file(workspace, "notes", "src/notes/runner.py", "GREETING = 'good morning'\n")

    second = _publish(workspace, runtime, "0.2.0")

    assert not second.is_repository_created
    assert second.parent == first.commit
    assert run_git(published, ["rev-list", "main"]).splitlines() == [second.commit, first.commit]
    assert run_git(published, ["rev-parse", "v0.1.0^{commit}", "v0.2.0^{commit}"]).splitlines() == [
        first.commit,
        second.commit,
    ]
    assert run_git(published, ["show", "main:src/notes/runner.py"]) == "GREETING = 'good morning'"
    assert runtime.calls.count("create_published_repository notes 'Notes'") == 1
    record = load_record(workspace, "notes")
    assert (record.published_commit, record.published_version) == (second.commit, "0.2.0")


def test_a_repository_someone_else_pushed_to_is_never_forced(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    first = _publish(workspace, runtime, "0.1.0")
    # Someone else's commit lands on the published main.
    other = tmp_path / "other"
    run_git(tmp_path, ["clone", "--quiet", str(published), str(other)])
    (other / "README.md").write_text("edited on GitHub\n")
    theirs = commit_all(other, "Edit the README on GitHub")
    run_git(other, ["push", "--quiet", "origin", "HEAD:main"])
    _versioned(workspace, runtime, "0.2.0")
    assemble_app(workspace, "notes", "HEAD", runtime)

    with pytest.raises(PushRefusedError, match="nothing is ever forced"):
        push_app(workspace, "notes", None, runtime)

    assert run_git(published, ["rev-parse", "main"]) == theirs
    assert "refs/tags/v0.2.0" not in _refs(published)
    assert load_record(workspace, "notes").published_commit == first.commit


def test_a_name_the_account_already_uses_stops_the_first_publish(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    runtime.taken_repository_names = ("notes",)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)

    with pytest.raises(PublishedNameTakenError, match="already exists"):
        push_app(workspace, "notes", None, runtime)

    assert _refs(published) == []
    assert load_record(workspace, "notes").published_repo_url == ""
    with pytest.raises(NotPushedError):
        record_publish(workspace, "notes", runtime)

    with pytest.raises(PublishError, match="not a repository name"):
        push_app(workspace, "notes", "-notes", runtime)

    # Another name, agreed at the scope gate, is taken up without assembling again.
    renamed = make_bare_repository(tmp_path / "github" / "octo-notes.git")
    point_url_at(workspace, "https://github.com/octo/octo-notes", renamed)
    pushed = push_app(workspace, "notes", "octo-notes", runtime)
    assert pushed.repo_url == "https://github.com/octo/octo-notes"
    assert run_git(renamed, ["rev-parse", "main"]) == pushed.commit


def test_a_repository_that_was_created_is_not_created_again_when_the_push_is_run_again(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    # The repository is created, but the push cannot reach it.
    point_url_at(workspace, _PUBLISHED_URL, tmp_path / "github" / "missing.git")
    run_git(workspace, ["config", "--unset", f"url.{published}.insteadOf"])
    with pytest.raises(PublishError, match="the push to https://github.com/octo/notes failed"):
        push_app(workspace, "notes", None, runtime)
    run_git(workspace, ["config", "--unset", f"url.{tmp_path / 'github' / 'missing.git'}.insteadOf"])
    point_url_at(workspace, _PUBLISHED_URL, published)

    pushed = push_app(workspace, "notes", None, runtime)
    again = push_app(workspace, "notes", None, runtime)

    assert not pushed.is_repository_created
    assert runtime.calls.count("create_published_repository notes 'Notes'") == 1
    # A push that already reached the repository makes no second commit.
    assert again.commit == pushed.commit
    assert run_git(published, ["rev-list", "--count", "main"]) == "1"


def test_only_the_first_publish_of_an_app_installed_and_changed_here_says_what_it_was_derived_from(
    tmp_path: Path,
) -> None:
    upstream = make_published_app_repository(tmp_path / "alice-notes", "notes", '\nversion = "0.3.0"\n')
    workspace = make_workspace_repository(tmp_path / "adopter")
    runtime = RecordingRuntime(repo_root=workspace, is_committing=True)
    installed = install_app(workspace, f"file://{upstream}", None, runtime, never_bound).record
    published = make_bare_repository(tmp_path / "github" / "notes.git")
    point_url_at(workspace, _PUBLISHED_URL, published)

    # Unchanged, it is its publisher's to publish.
    with pytest.raises(NotModifiedAppError, match="nothing of this workspace's to publish"):
        assemble_app(workspace, "notes", "HEAD", runtime)

    write_app_file(workspace, "notes", "README.md", "# notes, with a dark theme\n")
    first = _publish(workspace, runtime, "0.4.0")

    derived_from = f"file://{upstream}@{installed.upstream_commit}"
    assert first.derived_from == derived_from
    assert first.parent == ""
    assert run_git(published, ["log", "-1", "--format=%B", first.commit]) == (
        f"notes v0.4.0\n\nDerived-From: {derived_from}"
    )
    assert run_git(published, ["log", "-1", "--format=%(trailers:key=Derived-From,valueonly)", first.commit]) == (
        derived_from
    )

    write_app_file(workspace, "notes", "README.md", "# notes, with a dark theme and a light one\n")
    second = _publish(workspace, runtime, "0.5.0")

    assert second.derived_from == ""
    assert second.parent == first.commit
    assert run_git(published, ["log", "-1", "--format=%B", second.commit]) == "notes v0.5.0"


def test_an_app_built_here_is_derived_from_nothing(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)

    first = _publish(workspace, runtime, "0.1.0")

    assert first.derived_from == ""
    assert "Derived-From" not in run_git(published, ["log", "--format=%B", "main"])


def test_the_record_step_alone_finishes_a_publish_whose_record_was_not_written(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    pushed = push_app(workspace, "notes", None, runtime)
    # The push reached the repository and then the publish was interrupted: the record says nothing yet.
    assert load_record(workspace, "notes").published_commit == ""
    assert run_git(published, ["rev-parse", "main"]) == pushed.commit
    # A run that cannot commit writes the record and keeps what it needs to be run again.
    failing = RecordingRuntime(repo_root=workspace, commit_failure="fatal: unable to write new index file")
    interrupted = record_publish(workspace, "notes", failing)
    assert interrupted.problems == (
        "the published version is in the app's record but was not committed, so run this again: "
        "fatal: unable to write new index file",
    )

    recorded = record_publish(workspace, "notes", runtime)

    assert recorded.problems == ()
    record = load_record(workspace, "notes")
    assert record.published_repo_url == _PUBLISHED_URL
    assert record.published_commit == pushed.commit
    assert record.published_version == "0.1.0"
    assert record.verified_template == _TEMPLATE_VERSION
    log = run_git(workspace, ["log", "-1", "--format=%an <%ae>%n%s", "--name-only"]).splitlines()
    assert log[:2] == [
        "app-install <app-install@workspace.invalid>",
        "app-install: record that notes v0.1.0 is published",
    ]
    assert [line for line in log[2:] if line] == ["system/config/apps/notes.toml"]
    assert run_git(workspace, ["status", "--porcelain"]) == ""
    # Nothing waits any more, and nothing was pushed a second time.
    assert not pending_publish_path(workspace, "notes").exists()
    assert run_git(published, ["rev-list", "--count", "main"]) == "1"
    with pytest.raises(NotAssembledError):
        record_publish(workspace, "notes", runtime)


def test_recording_a_version_made_on_a_branch_that_was_not_merged_says_so(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    run_git(workspace, ["checkout", "--quiet", "-b", "mngr/publish-notes"])
    worker_commit = _versioned(workspace, runtime, "0.1.0")
    run_git(workspace, ["checkout", "--quiet", "main"])
    assemble_app(workspace, "notes", "mngr/publish-notes", runtime)
    push_app(workspace, "notes", None, runtime)

    recorded = record_publish(workspace, "notes", runtime)

    assert recorded.record.published_version == "0.1.0"
    assert len(recorded.problems) == 1
    assert worker_commit in recorded.problems[0] and "merge the branch" in recorded.problems[0]


def test_comparing_says_what_changed_since_the_last_publish(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, _EXCLUDES, is_python=False)
    write_app_file(workspace, "notes", "docs/guide.md", "how to use it\n")
    commit_all(workspace, "Add a guide")

    never = compare_with_published(workspace, "notes", "HEAD")

    assert never.record.published_commit == ""
    assert never.version == ""
    assert never.changed_paths == ()
    assert sorted(never.published.paths) == ["app.toml", "docs/guide.md", "icon.svg", "src/notes/runner.py"]

    _publish(workspace, runtime, "0.1.0")
    assert compare_with_published(workspace, "notes", "HEAD").changed_paths == ()

    write_app_file(workspace, "notes", "src/notes/runner.py", "GREETING = 'good morning'\n")
    write_app_file(workspace, "notes", "src/notes/search.py", "def search() -> None: ...\n")
    write_app_file(workspace, "notes", "docs/internal/roadmap.md", "private plans\n")
    (workspace / "system" / "apps" / "notes" / "docs" / "guide.md").unlink()
    commit_all(workspace, "Search, and a new greeting")

    changed = compare_with_published(workspace, "notes", "HEAD")

    assert changed.version == "0.1.0"
    assert changed.record.published_version == "0.1.0"
    # What the manifest excludes is no part of either version.
    assert changed.changed_paths == (
        ChangedPath(path="docs/guide.md", kind=ChangeKind.DELETED),
        ChangedPath(path="src/notes/runner.py", kind=ChangeKind.MODIFIED),
        ChangedPath(path="src/notes/search.py", kind=ChangeKind.ADDED),
    )
    assert changed.published.excluded_paths == ("docs/internal/roadmap.md",)


def test_a_published_commit_a_restored_workspace_lost_is_fetched_from_the_repository(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    first = _publish(workspace, runtime, "0.1.0")
    # A workspace restored from a backup of its files: the record names a commit its repository does not hold.
    restored = tmp_path / "restored"
    # Cloned by URL, which brings only what the branch reaches: the published commit is on no branch here.
    run_git(tmp_path, ["clone", "--quiet", "--no-tags", f"file://{workspace}", str(restored)])
    run_git(restored, ["tag", _TEMPLATE_VERSION, "HEAD~3"])
    point_url_at(restored, _PUBLISHED_URL, published)
    restored_runtime = RecordingRuntime(repo_root=restored, is_committing=True)
    assert not is_commit_present(restored, first.commit)
    write_app_file(restored, "notes", "src/notes/runner.py", "GREETING = 'restored'\n")

    second = _publish(restored, restored_runtime, "0.2.0")

    assert second.parent == first.commit
    assert run_git(published, ["rev-list", "main"]).splitlines() == [second.commit, first.commit]


def _install_scanners(workspace: Path, bin_directory: Path, finding_file: str | None) -> None:
    """Put the template's own secret scan script in the workspace, with stand-ins for its two scanners: each
    reports a finding in ``finding_file`` when that is given, in its own report format and with its own exit
    code, and finds nothing otherwise."""
    template_root = next(
        parent for parent in Path(__file__).resolve().parents if (parent / SECRET_SCAN_SCRIPT).is_file()
    )
    script = workspace / SECRET_SCAN_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text((template_root / SECRET_SCAN_SCRIPT).read_text())
    (script.parent / "betterleaks.toml").write_text("")
    bin_directory.mkdir(parents=True, exist_ok=True)
    betterleaks_finding = json.dumps(
        [{"RuleID": "generic-api-key", "File": "@TARGET@/" + (finding_file or ""), "StartLine": 1}]
    )
    kingfisher_finding = json.dumps(
        {"rule": {"name": "Generic API Key"}, "finding": {"path": "@TARGET@/" + (finding_file or ""), "line": 1}}
    )
    scanners = {
        "betterleaks": (
            'target="$2"\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "--report-path" ]; then report="$2"; fi\n  shift\ndone\n'
            + (
                f"printf '%s' '{betterleaks_finding}' | sed \"s|@TARGET@|$target|\" > \"$report\"\nexit 99\n"
                if finding_file
                else 'echo "[]" > "$report"\nexit 0\n'
            )
        ),
        "kingfisher": (
            'target="$2"\n'
            + (
                f"printf '%s\\n' '{kingfisher_finding}' | sed \"s|@TARGET@|$target|\"\nexit 200\n"
                if finding_file
                else "exit 0\n"
            )
        ),
    }
    for name, body in scanners.items():
        scanner = bin_directory / name
        scanner.write_text(f"#!/usr/bin/env bash\n{body}")
        scanner.chmod(scanner.stat().st_mode | stat.S_IXUSR)


def test_the_real_scan_reads_each_finding_with_its_file_from_the_templates_scan_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "settings.py").write_text("API_KEY = 'k'\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    runtime = WorkspaceRuntime(repo_root=workspace)

    _install_scanners(workspace, tmp_path / "bin", None)
    assert runtime.scan_secrets(tree).model_dump() == {"findings": (), "failure": ""}

    _install_scanners(workspace, tmp_path / "bin", "src/settings.py")
    found = runtime.scan_secrets(tree)
    assert found.failure == ""
    assert found.findings == (
        "[betterleaks] generic-api-key: src/settings.py:1 (value redacted)",
        "[kingfisher] Generic API Key: src/settings.py:1 (value redacted)",
    )

    # A scanner that is not installed is a scan that did not run, never a clean one.
    (tmp_path / "bin" / "kingfisher").unlink()
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    broken = runtime.scan_secrets(tree)
    assert broken.findings == ()
    assert "SCANNER MISSING: 'kingfisher'" in broken.failure


def test_the_command_runs_a_publish_step_by_step_and_names_each_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, published, runtime = _publisher(tmp_path, _EXCLUDES, is_python=False)
    _publish(workspace, runtime, "0.1.0")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    _install_scanners(workspace, tmp_path / "bin", None)
    commit_all(workspace, "Add the workspace's secret scan")
    write_app_file(workspace, "notes", "src/notes/search.py", "def search() -> None: ...\n")
    write_app_file(workspace, "notes", "notes.draft.md", "half written\n")
    commit_all(workspace, "Add search")
    arguments = ["--repo-root", str(workspace), "publish"]

    def run(*step: str) -> tuple[int, dict[str, object]]:
        result = CliRunner().invoke(main, [*arguments, *step])
        return result.exit_code, json.loads(result.output)

    assert run("push", "notes") == (
        1,
        {
            "result": "not_assembled",
            "name": "notes",
            "detail": "no assembled tree of notes is waiting: run `app-install publish assemble notes` first",
        },
    )

    exit_code, compared = run("compare", "notes")
    assert exit_code == 0
    assert compared["result"] == "compared"
    assert compared["is_published"] is True
    assert compared["published_version"] == "0.1.0"
    assert compared["origin"] == "local"
    assert compared["changed_paths"] == [{"path": "src/notes/search.py", "kind": "added"}]
    assert compared["excluded_paths"] == ["notes.draft.md"]

    exit_code, versioned = run("set-version", "notes", "0.2.0")
    assert (exit_code, versioned["result"], versioned["previous_version"]) == (0, "versioned", "0.1.0")
    assert versioned["verified_template"] == _TEMPLATE_VERSION
    assert versioned["changed_paths"] == ["system/apps/notes/app.toml"]
    commit_all(workspace, "notes 0.2.0")

    exit_code, assembled = run("assemble", "notes")
    assert exit_code == 0
    assert assembled["result"] == "assembled"
    assert assembled["version"] == "0.2.0"
    assert assembled["is_first_publish"] is False
    assert assembled["is_lock_checked"] is False
    assert assembled["excluded_paths"] == ["notes.draft.md"]
    assert assembled["needs"] == {
        "permissions": [],
        "secrets": [],
        "llm": None,
        "adaptation": [],
        "dependencies": [],
        "environment": {
            "apt": [],
            "npm_global": {},
            "uv_tools": {},
            "cargo_crates": {},
            "cargo_default_toolchain": None,
        },
    }

    assert run("record", "notes")[1]["result"] == "not_pushed"

    exit_code, pushed = run("push", "notes")
    assert exit_code == 0
    assert pushed["result"] == "pushed"
    assert pushed["published_repo_url"] == _PUBLISHED_URL
    assert pushed["tag"] == "v0.2.0"
    assert pushed["is_repository_created"] is False
    assert pushed["is_tag_pushed"] is True
    assert pushed["problems"] == []
    assert run_git(published, ["rev-parse", "main"]) == pushed["published_commit"]
    assert run_git(published, ["rev-parse", "main~1"]) == pushed["parent"]

    exit_code, recorded = run("record", "notes")
    assert exit_code == 0
    assert recorded == {
        "result": "recorded",
        "name": "notes",
        "published_repo_url": _PUBLISHED_URL,
        "published_commit": pushed["published_commit"],
        "published_version": "0.2.0",
        "verified_template": _TEMPLATE_VERSION,
        "problems": [],
    }

    # A secret in the next version stops it at the assembly, with the file.
    _install_scanners(workspace, tmp_path / "bin", "src/notes/search.py")
    run("set-version", "notes", "0.3.0")
    commit_all(workspace, "notes 0.3.0")
    exit_code, stopped = run("assemble", "notes")
    assert exit_code == 1
    assert stopped == {
        "result": "secrets_found",
        "name": "notes",
        "findings": [
            "[betterleaks] generic-api-key: src/notes/search.py:1 (value redacted)",
            "[kingfisher] Generic API Key: src/notes/search.py:1 (value redacted)",
        ],
    }
    assert run("push", "notes")[1]["result"] == "not_assembled"
    assert run_git(published, ["rev-parse", "main"]) == pushed["published_commit"]
