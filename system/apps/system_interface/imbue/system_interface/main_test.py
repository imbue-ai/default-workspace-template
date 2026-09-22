"""Tests for the main entry point."""

from pathlib import Path

from imbue.system_interface.app_context import state_of
from imbue.system_interface.config import Config
from imbue.system_interface.main import _parse_args
from imbue.system_interface.main import build_application
from imbue.system_interface.shell.state_files import DEFAULT_STATE_DIRECTORY


def test_build_application_defaults_to_the_workspace_state_directory() -> None:
    args = _parse_args([])
    app = build_application(Config(), args)
    assert state_of(app).shell.state_directory == DEFAULT_STATE_DIRECTORY


def test_build_application_threads_the_state_directory_through(tmp_path: Path) -> None:
    args = _parse_args(["--state-dir", str(tmp_path / "state")])
    app = build_application(Config(), args)
    assert state_of(app).shell.state_directory == tmp_path / "state"


def test_a_plain_boot_is_the_live_shell_and_preview_is_a_flag_of_its_own(tmp_path: Path) -> None:
    assert state_of(build_application(Config(), _parse_args([]))).is_preview is False
    args = _parse_args(["--preview", "--state-dir", str(tmp_path / "copy")])
    assert state_of(build_application(Config(), args)).is_preview is True
