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
from app_install.errors import OutsideSkillsError
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
from app_install.publishing import OutsideSkillUse
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
from app_install.testing import write_workspace_file
from app_manifest.records import load_record
from github_sync.testing import install_fake_latchkey

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


def test_a_pre_release_is_ordered_by_its_identifiers_with_a_number_read_as_a_number(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    _publish(workspace, runtime, "1.0.0-rc.9")

    for version in ("1.0.0-rc.9", "1.0.0-rc.2", "1.0.0-rc", "1.0.0-beta.10", "1.0.0-9"):
        with pytest.raises(PublishError, match="the next version must be newer"):
            set_version(workspace, "notes", version, runtime)

    for version in ("1.0.0-rc.10", "1.0.0-rc.9.1", "1.0.0-rd", "1.0.0"):
        assert set_version(workspace, "notes", version, runtime).version == version


def test_a_version_that_cannot_be_written_everywhere_is_written_nowhere(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path)
    manifest_file = workspace / "system" / "apps" / "notes" / "app.toml"
    project_file = workspace / "system" / "apps" / "notes" / "pyproject.toml"
    # A project file with no table to carry the version: the manifest is written before that is found out.
    project_file.write_text('[tool.notes]\nkind = "demo"\n')
    manifest_before = manifest_file.read_text()

    with pytest.raises(PublishError, match=r"has no \[project\] table"):
        set_version(workspace, "notes", "0.1.0", runtime)

    assert manifest_file.read_text() == manifest_before
    assert project_file.read_text() == '[tool.notes]\nkind = "demo"\n'
    assert "relock_app notes" not in runtime.calls


def test_a_lock_that_is_not_made_again_is_a_problem_of_a_version_that_was_written(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path)
    runtime.relock_failure = "uv lock exited 2"

    outcome = set_version(workspace, "notes", "0.1.0", runtime)

    assert outcome.version == "0.1.0"
    assert outcome.changed_paths == ("system/apps/notes/app.toml", "system/apps/notes/pyproject.toml")
    assert outcome.problems == (
        "the app's lock was not made again, and it names the app's own version: uv lock exited 2",
    )
    assert 'version = "0.1.0"' in (workspace / "system" / "apps" / "notes" / "app.toml").read_text()


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


_NAMES_FETCH_MAIL = 'COMMAND = ["uv", "run", ".agents/skills/fetch-mail/scripts/run.py"]\nSTORE = "data/.skills/fetch-mail/mail.db"\n'


def test_a_tree_that_names_a_skill_the_app_does_not_carry_is_not_assembled(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    write_workspace_file(workspace, ".agents/skills/fetch-mail/SKILL.md", "# Fetch mail\n")
    write_app_file(workspace, "notes", "src/notes/refresh.py", _NAMES_FETCH_MAIL)
    _versioned(workspace, runtime, "0.1.0")

    compared = compare_with_published(workspace, "notes", "HEAD")
    with pytest.raises(OutsideSkillsError) as stopped:
        assemble_app(workspace, "notes", "HEAD", runtime)

    assert compared.is_outside_skills_checked
    assert compared.outside_skill_uses == (
        OutsideSkillUse(skill="fetch-mail", path="src/notes/refresh.py", line=1),
        OutsideSkillUse(skill="fetch-mail", path="src/notes/refresh.py", line=2),
    )
    assert stopped.value.uses == (
        "src/notes/refresh.py:1 names the skill fetch-mail, which this workspace holds and the app does not carry",
        "src/notes/refresh.py:2 names the skill fetch-mail, which this workspace holds and the app does not carry",
    )
    assert not pending_publish_path(workspace, "notes").exists()
    assert _refs(published) == []

    # The app carrying the skill is the way on: the skill is then in what is published.
    (workspace / ".agents" / "skills" / "fetch-mail" / "SKILL.md").unlink()
    write_app_file(workspace, "notes", "skills/fetch-mail/SKILL.md", "# Fetch mail\n")
    commit_all(workspace, "Have the notes app carry the skill it runs")
    assembled = assemble_app(workspace, "notes", "HEAD", runtime)
    assert assembled.is_outside_skills_checked
    assert "skills/fetch-mail/SKILL.md" in assembled.published.paths
    assert compare_with_published(workspace, "notes", "HEAD").outside_skill_uses == ()


def test_a_skill_the_template_ships_and_a_path_that_names_no_skill_are_no_reason_to_stop(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    write_workspace_file(workspace, ".agents/skills/notify-user/SKILL.md", "# Notify the user\n")
    commit_all(workspace, "A later template release, which ships the notify-user skill")
    run_git(workspace, ["tag", "minds-v0.11.1"])
    write_app_file(
        workspace,
        "notes",
        "src/notes/refresh.py",
        'NOTIFY = ".agents/skills/notify-user/python/notify.py"\nEXAMPLE = ".agents/skills/some-skill/run.py"\n',
    )
    _versioned(workspace, runtime, "0.1.0")

    assert compare_with_published(workspace, "notes", "HEAD").outside_skill_uses == ()
    assert assemble_app(workspace, "notes", "HEAD", runtime).is_outside_skills_checked


def test_a_workspace_with_no_template_release_cannot_tell_its_own_skills_from_the_templates(tmp_path: Path) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    run_git(workspace, ["tag", "--delete", _TEMPLATE_VERSION])
    write_workspace_file(workspace, ".agents/skills/fetch-mail/SKILL.md", "# Fetch mail\n")
    write_app_file(workspace, "notes", "src/notes/refresh.py", _NAMES_FETCH_MAIL)
    _versioned(workspace, runtime, "0.1.0")

    compared = compare_with_published(workspace, "notes", "HEAD")
    assembled = assemble_app(workspace, "notes", "HEAD", runtime)

    assert (compared.is_outside_skills_checked, compared.outside_skill_uses) == (False, ())
    assert not assembled.is_outside_skills_checked


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
    # The record names the repository from the moment it exists; the version waits for the record step.
    assert pushed.record.published_repo_url == _PUBLISHED_URL
    assert (pushed.record.published_commit, pushed.record.published_version) == ("", "")
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
    assert load_record(workspace, "notes").published_repo_url == "https://github.com/octo/octo-notes"


def test_a_repository_that_was_created_is_not_created_again_when_the_push_is_run_again(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    # The repository is created, but the push cannot reach it.
    point_url_at(workspace, _PUBLISHED_URL, tmp_path / "github" / "missing.git")
    run_git(workspace, ["config", "--unset", f"url.{published}.insteadOf"])
    with pytest.raises(PublishError, match="the push to https://github.com/octo/notes failed"):
        push_app(workspace, "notes", None, runtime)
    # The repository exists from then on, so the record names it, committed, before anything is in it.
    assert load_record(workspace, "notes").published_repo_url == _PUBLISHED_URL
    assert run_git(workspace, ["log", "-1", "--format=%an|%s"]) == (
        "app-install|app-install: record the published repository of notes"
    )
    assert run_git(workspace, ["status", "--porcelain"]) == ""
    # Assembling again, after a fix say, does not forget the repository either.
    assemble_app(workspace, "notes", "HEAD", runtime)
    run_git(workspace, ["config", "--unset", f"url.{tmp_path / 'github' / 'missing.git'}.insteadOf"])
    point_url_at(workspace, _PUBLISHED_URL, published)

    pushed = push_app(workspace, "notes", None, runtime)
    again = push_app(workspace, "notes", None, runtime)

    assert not pushed.is_repository_created
    assert runtime.calls.count("create_published_repository notes 'Notes'") == 1
    # A push that already reached the repository makes no second commit.
    assert again.commit == pushed.commit
    assert run_git(published, ["rev-list", "--count", "main"]) == "1"


def test_a_push_that_reached_the_repository_before_that_was_noted_is_finished_by_running_it_again(
    tmp_path: Path,
) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    pushed = push_app(workspace, "notes", None, runtime)
    # As if the run had been cut off right after git pushed: the commit is noted, that it arrived is not.
    pending_file = pending_publish_path(workspace, "notes")
    noted = json.loads(pending_file.read_text())
    assert noted["commit"] == pushed.commit
    pending_file.write_text(json.dumps({**noted, "is_commit_pushed": False, "is_tag_pushed": False}))

    again = push_app(workspace, "notes", None, runtime)

    # The same commit is pushed again, which the repository already has, so nothing is refused or doubled.
    assert again.commit == pushed.commit
    assert again.is_tag_pushed
    assert again.problems == ()
    assert run_git(published, ["rev-list", "main"]) == pushed.commit
    assert record_publish(workspace, "notes", runtime).record.published_commit == pushed.commit


def _refuse_tags(published: Path) -> Path:
    """Have the repository refuse every tag pushed to it, and return the hook that does."""
    hook = published / "hooks" / "update"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\ncase "$1" in refs/tags/*) echo "no tags for now" >&2; exit 1 ;; esac\n')
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hook


def test_a_tag_that_did_not_reach_the_repository_is_sent_alone_by_pushing_again(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    hook = _refuse_tags(published)

    untagged = push_app(workspace, "notes", None, runtime)

    # The version is published all the same, and the outcome says its tag is still owed.
    assert untagged.is_tag_pushed is False
    (problem,) = untagged.problems
    assert problem.startswith(f"{untagged.commit} is published, but its tag v0.1.0 was not pushed")
    assert _refs(published) == ["refs/heads/main"]
    assert run_git(published, ["rev-parse", "main"]) == untagged.commit

    hook.unlink()
    tagged = push_app(workspace, "notes", None, runtime)

    assert (tagged.is_tag_pushed, tagged.commit, tagged.problems) == (True, untagged.commit, ())
    assert run_git(published, ["rev-list", "--count", "main"]) == "1"
    assert run_git(published, ["rev-parse", "v0.1.0^{commit}"]) == untagged.commit
    assert runtime.calls.count("create_published_repository notes 'Notes'") == 1


def test_a_tag_the_repository_already_has_is_reported_and_never_moved(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    first = _publish(workspace, runtime, "0.1.0")
    # Someone tagged the first version as the next one on GitHub.
    run_git(published, ["tag", "v0.2.0", first.commit])
    write_app_file(workspace, "notes", "src/notes/runner.py", "GREETING = 'good morning'\n")
    _versioned(workspace, runtime, "0.2.0")
    assemble_app(workspace, "notes", "HEAD", runtime)

    second = push_app(workspace, "notes", None, runtime)

    assert second.is_tag_pushed is False
    (problem,) = second.problems
    assert problem.startswith(f"{_PUBLISHED_URL} already has a tag v0.2.0, which is never moved")
    assert run_git(published, ["rev-parse", "main"]) == second.commit
    assert run_git(published, ["rev-parse", "v0.2.0^{commit}"]) == first.commit


def test_a_repository_that_was_not_closed_or_not_given_its_topic_is_a_problem_of_the_first_publish(
    tmp_path: Path,
) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    runtime.lockdown_problem = "PATCH answered 403"
    runtime.topic_problem = "the topic was not set"
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)

    pushed = push_app(workspace, "notes", None, runtime)

    assert pushed.is_repository_created and pushed.is_tag_pushed
    assert pushed.problems == (
        f"closing {_PUBLISHED_URL} to outside contributions (issues, wiki, projects, discussions, outside "
        "pull requests) did not take: PATCH answered 403",
        f"{_PUBLISHED_URL} was not given the topic published apps are found by: the topic was not set",
    )
    assert run_git(published, ["rev-parse", "main"]) == pushed.commit


def test_a_first_publish_whose_push_fails_still_says_what_creating_the_repository_could_not_do(
    tmp_path: Path,
) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    runtime.lockdown_problem = "PATCH answered 403"
    runtime.topic_problem = "the topic was not set"
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    # The repository is created, but the push cannot reach it.
    run_git(workspace, ["config", "--unset", f"url.{published}.insteadOf"])
    point_url_at(workspace, _PUBLISHED_URL, tmp_path / "github" / "missing.git")

    with pytest.raises(PublishError) as failure:
        push_app(workspace, "notes", None, runtime)

    # The run after this one finds the repository made, so this is the only time either is said.
    assert f"the push to {_PUBLISHED_URL} failed" in str(failure.value)
    assert "did not take: PATCH answered 403" in str(failure.value)
    assert "the topic published apps are found by: the topic was not set" in str(failure.value)
    run_git(workspace, ["config", "--unset", f"url.{tmp_path / 'github' / 'missing.git'}.insteadOf"])
    point_url_at(workspace, _PUBLISHED_URL, published)
    assert push_app(workspace, "notes", None, runtime).problems == ()


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


def test_the_next_version_is_not_assembled_while_a_pushed_one_waits_to_be_recorded(tmp_path: Path) -> None:
    workspace, published, runtime = _publisher(tmp_path, is_python=False)
    _versioned(workspace, runtime, "0.1.0")
    assemble_app(workspace, "notes", "HEAD", runtime)
    pushed = push_app(workspace, "notes", None, runtime)
    _versioned(workspace, runtime, "0.2.0")

    # Assembling now would forget which commit was pushed, and the next push could not follow it.
    with pytest.raises(PublishError, match="publish record notes"):
        assemble_app(workspace, "notes", "HEAD", runtime)

    record_publish(workspace, "notes", runtime)
    assemble_app(workspace, "notes", "HEAD", runtime)
    second = push_app(workspace, "notes", None, runtime)
    assert second.parent == pushed.commit
    assert run_git(published, ["rev-list", "main"]).splitlines() == [second.commit, pushed.commit]


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


def test_the_command_names_each_line_that_names_a_skill_the_app_does_not_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _published, runtime = _publisher(tmp_path, is_python=False)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    _install_scanners(workspace, tmp_path / "bin", None)
    write_workspace_file(workspace, ".agents/skills/fetch-mail/SKILL.md", "# Fetch mail\n")
    write_app_file(workspace, "notes", "refresh.sh", "uv run .agents/skills/fetch-mail/scripts/run.py\n")
    _versioned(workspace, runtime, "0.1.0")
    arguments = ["--repo-root", str(workspace), "publish"]

    compared = CliRunner().invoke(main, [*arguments, "compare", "notes"])
    assembled = CliRunner().invoke(main, [*arguments, "assemble", "notes"])

    comparison = json.loads(compared.output)
    assert (compared.exit_code, comparison["result"], comparison["is_outside_skills_checked"]) == (0, "compared", True)
    assert comparison["outside_skill_uses"] == [{"skill": "fetch-mail", "path": "refresh.sh", "line": 1}]
    refusal = json.loads(assembled.output)
    assert (assembled.exit_code, refusal["result"], refusal["name"]) == (1, "outside_skills_found", "notes")
    assert refusal["uses"] == [
        "refresh.sh:1 names the skill fetch-mail, which this workspace holds and the app does not carry"
    ]


def test_the_command_names_a_lock_that_does_not_hold_a_name_that_is_taken_and_a_push_that_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, published, runtime = _publisher(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    _install_scanners(workspace, tmp_path / "bin", None)
    commit_all(workspace, "Add the workspace's secret scan")
    arguments = ["--repo-root", str(workspace), "publish"]

    def run(*step: str) -> tuple[int, dict[str, object]]:
        result = CliRunner().invoke(main, [*arguments, *step])
        return result.exit_code, json.loads(result.output)

    # The stand-in relock writes a lock no project resolves to, so the real check, run on the assembled tree
    # outside the workspace, does not hold.
    _versioned(workspace, runtime, "0.1.0")
    exit_code, unlocked = run("assemble", "notes")
    assert (exit_code, unlocked["result"], unlocked["name"]) == (1, "lock_check_failed", "notes")
    assert "uv.lock" in str(unlocked["detail"])
    assert run("push", "notes")[1]["result"] == "not_assembled"

    # Without its project the app has nothing to lock. The account already has a repository of its name.
    (workspace / "system" / "apps" / "notes" / "pyproject.toml").unlink()
    commit_all(workspace, "Drop the project")
    assert run("assemble", "notes")[1]["result"] == "assembled"
    install_fake_latchkey(
        tmp_path / "bin", """echo '{"errors": [{"message": "name already exists on this account"}]}'"""
    )
    exit_code, taken = run("push", "notes", "--repo-name", "octo-notes")
    assert (exit_code, taken["result"], taken["repo_name"]) == (1, "name_taken", "octo-notes")
    assert load_record(workspace, "notes").published_repo_url == ""
    assert _refs(published) == []

    # Published once, then someone else's commit lands on the published main.
    pushed = push_app(workspace, "notes", None, runtime)
    record_publish(workspace, "notes", runtime)
    other = tmp_path / "other"
    run_git(tmp_path, ["clone", "--quiet", str(published), str(other)])
    (other / "README.md").write_text("edited on GitHub\n")
    theirs = commit_all(other, "Edit the README on GitHub")
    run_git(other, ["push", "--quiet", "origin", "HEAD:main"])
    _versioned(workspace, runtime, "0.2.0")
    assert run("assemble", "notes")[1]["result"] == "assembled"
    exit_code, refused = run("push", "notes")
    assert (exit_code, refused["result"], refused["name"]) == (1, "push_refused", "notes")
    assert "nothing is ever forced" in str(refused["detail"])
    assert run_git(published, ["rev-parse", "main"]) == theirs
    assert load_record(workspace, "notes").published_commit == pushed.commit
