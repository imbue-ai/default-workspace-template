from collections.abc import Generator
from pathlib import Path

import pytest

from routines.chat_app import HttpChatApp
from routines.definitions import FileDefinitionStore
from routines.ledger import FileLedger
from routines.migrate import LEGACY_CRON_DIR
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_http_server_test import FakeHttpServer
from routines.mock_http_server_test import serving_fake_http_server
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_repository_test import RecordingRepository
from routines.paths import RoutinesPaths
from routines.repository import GitRepository
from routines.runner import Runner
from routines.state import FileStateStore
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE
from routines.testing import init_git_workspace
from routines.testing import make_workspace_paths


@pytest.fixture
def paths(tmp_path: Path) -> RoutinesPaths:
    return make_workspace_paths(tmp_path / "workspace")


@pytest.fixture
def git_paths(paths: RoutinesPaths) -> RoutinesPaths:
    """The same root as a git repository with one commit, for the stores that commit."""
    return init_git_workspace(paths)


@pytest.fixture
def legacy_cron_dir(paths: RoutinesPaths) -> Path:
    """The durable copy of the cron entries a workspace wrote before routines existed, under the same root."""
    directory = paths.repo_root / LEGACY_CRON_DIR
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def git_repository(git_paths: RoutinesPaths, clock: FakeClock) -> GitRepository:
    return GitRepository(repo_root=git_paths.repo_root, clock=clock)


@pytest.fixture
def written_definition(git_paths: RoutinesPaths) -> Path:
    """A definition file written and not yet committed."""
    path = git_paths.definitions_dir / "digest.toml"
    path.write_text('title = "x"\n')
    return path


@pytest.fixture
def live_cron_dir(tmp_path: Path) -> Path:
    """What stands in for /etc/cron.d in a migration test."""
    directory = tmp_path / "etc-cron-d"
    directory.mkdir()
    return directory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(current=TEST_NOW, zone=TEST_ZONE)


@pytest.fixture
def repository(paths: RoutinesPaths) -> RecordingRepository:
    return RecordingRepository(repo_root=paths.repo_root)


@pytest.fixture
def definitions(paths: RoutinesPaths, repository: RecordingRepository) -> FileDefinitionStore:
    return FileDefinitionStore(paths=paths, repository=repository)


@pytest.fixture
def states(paths: RoutinesPaths) -> FileStateStore:
    return FileStateStore(paths=paths)


@pytest.fixture
def ledger(paths: RoutinesPaths) -> FileLedger:
    return FileLedger(paths=paths)


@pytest.fixture
def chat_app() -> ScriptedChatApp:
    return ScriptedChatApp()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def runner(
    paths: RoutinesPaths,
    definitions: FileDefinitionStore,
    states: FileStateStore,
    ledger: FileLedger,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    clock: FakeClock,
) -> Runner:
    return Runner(
        paths=paths,
        definitions=definitions,
        states=states,
        ledger=ledger,
        chat_app=chat_app,
        notifier=notifier,
        clock=clock,
    )


@pytest.fixture
def http_chat_app(tmp_path: Path, http_server: FakeHttpServer, clock: FakeClock) -> HttpChatApp:
    """The chat app over the fake server, found through a registry that names it."""
    registry = tmp_path / "apps.toml"
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{http_server.url}"\n')
    return HttpChatApp(repo_root=tmp_path, registry_path=registry, environ={}, clock=clock)


@pytest.fixture
def http_server() -> Generator[FakeHttpServer, None, None]:
    with serving_fake_http_server() as server:
        yield server
