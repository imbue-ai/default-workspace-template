from pathlib import Path

import pytest
from flask.testing import FlaskClient

from routines.data_types import RoutineDefinition
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_repository_test import RecordingRepository
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId
from routines.service import RoutinesService
from routines.state import FileStateStore
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE
from routines.testing import build_test_service
from routines.testing import init_git_workspace
from routines.testing import make_workspace_paths
from routines_app.mock_fix_chat_test import RecordingFixChatStarter
from routines_app.server import create_app


@pytest.fixture
def git_paths(tmp_path: Path) -> RoutinesPaths:
    return init_git_workspace(make_workspace_paths(tmp_path / "workspace"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(current=TEST_NOW, zone=TEST_ZONE)


@pytest.fixture
def chat_app() -> ScriptedChatApp:
    return ScriptedChatApp()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def repository(git_paths: RoutinesPaths) -> RecordingRepository:
    """What the service commits through: records each commit, and refuses them all once told to."""
    return RecordingRepository(repo_root=git_paths.repo_root)


@pytest.fixture
def service(
    git_paths: RoutinesPaths,
    repository: RecordingRepository,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> RoutinesService:
    return build_test_service(git_paths, repository, FileStateStore(paths=git_paths), clock, chat_app, notifier)


@pytest.fixture
def sync_routine(service: RoutinesService) -> RoutineId:
    """One hourly command routine, news-sync, added to the service."""
    routine_id = RoutineId("news-sync")
    values: dict[str, object] = {
        "title": "News sync",
        "what": "Pulls the headlines.",
        "every": "1h",
        "command": ["sh", "-c", "echo ran"],
    }
    service.add(routine_id, RoutineDefinition.model_validate(values))
    return routine_id


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><head><title>Routines</title></head><div id=app></div>")
    (directory / "assets" / "app.js").write_text("console.log('built');")
    (directory / "assets" / "app-Ab12cd34.js").write_text("console.log('built');")
    return directory


@pytest.fixture
def fix_chat_starter() -> RecordingFixChatStarter:
    return RecordingFixChatStarter()


@pytest.fixture
def client(service: RoutinesService, static_directory: Path, fix_chat_starter: RecordingFixChatStarter) -> FlaskClient:
    return create_app(service, static_directory, is_preview=False, fix_chat_starter=fix_chat_starter).test_client()


@pytest.fixture
def preview_client(
    service: RoutinesService, static_directory: Path, fix_chat_starter: RecordingFixChatStarter
) -> FlaskClient:
    return create_app(service, static_directory, is_preview=True, fix_chat_starter=fix_chat_starter).test_client()
