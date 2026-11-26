from pathlib import Path

import pytest
from workspace_profile.timezone import read_user_timezone

from workspace_initial_data.cli import HOST_DIR_ENV_VAR, main
from workspace_initial_data.status import InitialDataState, read_initial_data_status
from workspace_initial_data.testing import (
    initial_data_document,
    write_initial_data_document,
)


def test_the_step_applies_the_document_in_the_host_directory_to_the_workspace_it_runs_from(
    workspace_root: Path, host_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_DIR_ENV_VAR, str(host_dir))
    document_path = write_initial_data_document(
        host_dir, initial_data_document(backups=None)
    )

    main()

    assert read_initial_data_status(workspace_root).state is InitialDataState.APPLIED
    assert read_user_timezone(workspace_root) == "Europe/Berlin"
    assert not document_path.exists()


def test_the_step_does_nothing_where_no_host_directory_is_named(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOST_DIR_ENV_VAR, raising=False)

    main()

    assert not (workspace_root / "data").exists()
