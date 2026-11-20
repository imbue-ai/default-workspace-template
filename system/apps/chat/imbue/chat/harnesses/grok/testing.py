"""Test utilities for the grok harness: the captured grok sessions under ``fixtures/``."""

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.grok.session_parser import UPDATES_FILENAME

GROK_FIXTURES_DIR: Final[Path] = Path(__file__).parent / "fixtures"
# The directories of captured scenarios: mngr's TUI captures, then the chat app's TUI and headless ones.
FIXTURE_GROUP_DIRS: Final[tuple[Path, ...]] = tuple(
    GROK_FIXTURES_DIR / group for group in ("sessions", "tui_sessions", "headless_sessions")
)
# A scenario directory's list of its top-level session ids, one per line.
FIXTURE_ROOTS_FILENAME: Final[str] = "roots"


def find_fixture_scenario(name: str) -> Path:
    """The directory of the captured scenario ``name``, from the first fixture group that has it."""
    scenario_dirs = [group_dir / name for group_dir in FIXTURE_GROUP_DIRS]
    return next((scenario_dir for scenario_dir in scenario_dirs if scenario_dir.is_dir()), scenario_dirs[-1])


def list_fixture_scenarios() -> list[Path]:
    return sorted(path for group_dir in FIXTURE_GROUP_DIRS for path in group_dir.iterdir() if path.is_dir())


def read_fixture_root_ids(scenario_dir: Path) -> list[str]:
    return scenario_dir.joinpath(FIXTURE_ROOTS_FILENAME).read_text().split()


def read_fixture_session_ids(scenario_dir: Path) -> list[str]:
    """Every session the scenario captured: its top-level sessions in order, then its child sessions."""
    root_ids = read_fixture_root_ids(scenario_dir)
    child_ids = sorted(path.name for path in scenario_dir.iterdir() if path.is_dir() and path.name not in root_ids)
    return [*root_ids, *child_ids]


def read_fixture_updates(scenario_dir: Path, session_id: str) -> bytes:
    return (scenario_dir / session_id / UPDATES_FILENAME).read_bytes()
