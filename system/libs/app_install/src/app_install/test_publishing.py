"""An app from its publisher's workspace to an adopter's and back for the next version: published, installed,
changed by the adopter, published again, and updated, with a local bare repository standing in for the published
repository on GitHub."""

from pathlib import Path

from app_install.git import run_git
from app_install.installing import install_app
from app_install.mock_runtime_test import RecordingRuntime
from app_install.operations import AppState
from app_install.operations import add_app
from app_install.operations import check
from app_install.operations import list_apps
from app_install.publishing import assemble_app
from app_install.publishing import push_app
from app_install.publishing import record_publish
from app_install.publishing import set_version
from app_install.testing import commit_all
from app_install.testing import make_bare_repository
from app_install.testing import make_workspace_repository
from app_install.testing import never_bound
from app_install.testing import point_url_at
from app_install.testing import write_app_file
from app_install.testing import write_user_app
from app_install.updating import UpdateResult
from app_install.updating import update_app
from app_manifest.records import AppOrigin
from app_manifest.records import load_record

_PUBLISHED_URL = "https://github.com/octo/notes"
_EXCLUDED_NOTE = "what the publisher keeps to themselves\n"


def _notes_module(greeting: str, limit: int) -> str:
    """The app's one module: a greeting on its first line and a limit on its last, far enough apart that a change
    to each merges."""
    middle = "".join(f"# line {number}\n" for number in range(1, 9))
    return f"GREETING = {greeting!r}\n{middle}LIMIT = {limit}\n"


def _publish(workspace: Path, runtime: RecordingRuntime, version: str) -> str:
    """Run a publish's steps in order, as the publish-app skill does; the published commit."""
    set_version(workspace, "notes", version, runtime)
    commit_all(workspace, f"notes {version}")
    assemble_app(workspace, "notes", "HEAD", runtime)
    pushed = push_app(workspace, "notes", None, runtime)
    recorded = record_publish(workspace, "notes", runtime)
    assert (pushed.problems, recorded.problems) == ((), ())
    return pushed.commit


def test_an_app_is_published_installed_published_again_and_updated_over_the_adopters_own_edit(tmp_path: Path) -> None:
    published = make_bare_repository(tmp_path / "github" / "notes.git")
    publisher = make_workspace_repository(tmp_path / "publisher")
    point_url_at(publisher, _PUBLISHED_URL, published)
    package = write_user_app(publisher, "notes", '\n[publish]\nexclude = ["docs/internal/**"]\n')
    write_app_file(publisher, package, "src/notes/runner.py", _notes_module("hello", 10))
    write_app_file(publisher, package, "docs/internal/plans.md", _EXCLUDED_NOTE)
    add_app(publisher, package, None, never_bound)
    commit_all(publisher, "Add the notes app")
    publisher_runtime = RecordingRuntime(repo_root=publisher, is_committing=True)

    first = _publish(publisher, publisher_runtime, "0.1.0")

    # The adopter is given the repository's address and nothing else.
    adopter = make_workspace_repository(tmp_path / "adopter")
    point_url_at(adopter, _PUBLISHED_URL, published)
    adopter_runtime = RecordingRuntime(repo_root=adopter, is_committing=True)
    installed = install_app(adopter, _PUBLISHED_URL, None, adopter_runtime, never_bound)

    assert installed.ref == "v0.1.0"
    assert installed.record.origin is AppOrigin.INSTALLED
    assert (installed.record.upstream_commit, installed.record.upstream_version) == (first, "0.1.0")
    adopted_module = adopter / "system" / "apps" / "notes" / "src" / "notes" / "runner.py"
    assert adopted_module.read_text() == _notes_module("hello", 10)
    # What the publisher excluded never reached the adopter, and what derives from the manifest was made here.
    assert not (adopter / "system" / "apps" / "notes" / "docs").exists()
    assert (adopter / "system" / "supervisord.conf.d" / "notes.conf").is_file()
    assert check(adopter) == ()

    # The adopter makes the app their own, in a line the publisher's next version does not touch.
    adopted_module.write_text(_notes_module("hello", 25))
    commit_all(adopter, "Show more notes at once")
    (adopter_app,) = list_apps(adopter)
    assert adopter_app.state is AppState.MODIFIED

    # The publisher's next version changes another line of the same file.
    write_app_file(publisher, package, "src/notes/runner.py", _notes_module("good morning", 10))
    write_app_file(publisher, package, "src/notes/search.py", "def search() -> None: ...\n")
    second = _publish(publisher, publisher_runtime, "0.2.0")

    assert run_git(published, ["rev-list", "main"]).splitlines() == [second, first]

    updated = update_app(adopter, "notes", None, False, adopter_runtime)

    assert updated.result is UpdateResult.UPDATED
    assert (updated.previous_version, updated.ref) == ("0.1.0", "v0.2.0")
    # The new version is in, and the adopter's own edit is still there.
    assert adopted_module.read_text() == _notes_module("good morning", 25)
    assert (adopter / "system" / "apps" / "notes" / "src" / "notes" / "search.py").is_file()
    record = load_record(adopter, "notes")
    assert (record.upstream_commit, record.upstream_version) == (second, "0.2.0")
    assert run_git(adopter, ["rev-parse", "refs/app-upstream/notes"]) == second
    assert check(adopter) == ()
    # Still changed here, so still the adopter's own to keep in a repository or to publish.
    (adopter_app,) = list_apps(adopter)
    assert adopter_app.state is AppState.MODIFIED
    assert _EXCLUDED_NOTE not in run_git(adopter, ["log", "--all", "-p", "--format="])
