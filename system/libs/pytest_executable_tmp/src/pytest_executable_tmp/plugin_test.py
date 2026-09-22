from pathlib import Path

import pytest

from pytest_executable_tmp.plugin import can_run_files_in, select_temp_root

_DEFAULT = Path("/default-root-51873")
_FALLBACK = Path("/fallback-root-51873")
_EXPLICIT = Path("/explicit-root-51873")


@pytest.mark.parametrize(
    ("explicit_root", "runnable", "expected_usable", "expected_rejected"),
    [
        pytest.param(
            None, (_DEFAULT, _FALLBACK), _DEFAULT, (), id="keeps-a-runnable-default"
        ),
        pytest.param(
            None,
            (_FALLBACK,),
            _FALLBACK,
            (_DEFAULT,),
            id="moves-off-an-unrunnable-default",
        ),
        pytest.param(
            _EXPLICIT,
            (_DEFAULT, _FALLBACK),
            None,
            (_EXPLICIT,),
            id="never-replaces-an-explicit-root",
        ),
        pytest.param(
            None, (), None, (_DEFAULT, _FALLBACK), id="nothing-when-no-candidate-runs"
        ),
    ],
)
def test_select_temp_root_picks_the_first_candidate_that_can_run_files(
    explicit_root: Path | None,
    runnable: tuple[Path, ...],
    expected_usable: Path | None,
    expected_rejected: tuple[Path, ...],
) -> None:
    selection = select_temp_root(
        _DEFAULT, explicit_root, _FALLBACK, lambda directory: directory in runnable
    )

    assert selection.usable_root == expected_usable
    assert selection.rejected_roots == expected_rejected


def test_can_run_files_in_runs_a_script_in_a_directory_that_allows_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-yet-created"

    assert can_run_files_in(root)
    assert list(root.iterdir()) == []


def test_can_run_files_in_is_false_where_nothing_can_be_written(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("")

    assert not can_run_files_in(not_a_directory)
