from pathlib import Path

from workspace_backup_setup.verification import BACKUP_TOML_RELATIVE_PATH
from workspace_backup_setup.verification import read_verification_disabled
from workspace_backup_setup.verification import render_backup_toml_with_flag
from workspace_backup_setup.verification import write_verification_disabled


def test_a_workspace_with_no_backup_config_reads_verification_as_on(tmp_path: Path) -> None:
    assert read_verification_disabled(tmp_path) is False


def test_the_flag_is_written_and_read_back(tmp_path: Path) -> None:
    write_verification_disabled(tmp_path, is_disabled=True)
    assert read_verification_disabled(tmp_path) is True
    assert (tmp_path / BACKUP_TOML_RELATIVE_PATH).read_text() == "verification_disabled = true\n"

    write_verification_disabled(tmp_path, is_disabled=False)
    assert read_verification_disabled(tmp_path) is False


def test_every_other_line_of_the_users_file_is_kept_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / BACKUP_TOML_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        "# my backup settings\ninterval_seconds = 900\nverification_disabled = false\n\n[retention]\n"
        "verification_disabled = 'a table own key'\nkeep_daily = 7\n"
    )

    write_verification_disabled(tmp_path, is_disabled=True)

    assert path.read_text() == (
        "verification_disabled = true\n# my backup settings\ninterval_seconds = 900\n\n[retention]\n"
        "verification_disabled = 'a table own key'\nkeep_daily = 7\n"
    )
    assert read_verification_disabled(tmp_path) is True


def test_an_unreadable_config_reads_verification_as_on(tmp_path: Path) -> None:
    path = tmp_path / BACKUP_TOML_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("this is = not [ toml")

    assert read_verification_disabled(tmp_path) is False


def test_rendering_into_an_empty_file_yields_the_flag_line_alone() -> None:
    assert render_backup_toml_with_flag("", is_disabled=False) == "verification_disabled = false\n"
