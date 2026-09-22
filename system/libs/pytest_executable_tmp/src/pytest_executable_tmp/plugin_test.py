from pathlib import Path

from pytest_executable_tmp.plugin import can_run_files_in, select_temp_root

_DEFAULT = Path("/default-root-51873")
_FALLBACK = Path("/fallback-root-51873")
_EXPLICIT = Path("/explicit-root-51873")


def _runnable_only(*runnable: Path):
    return lambda directory: directory in runnable


def test_select_temp_root_keeps_a_default_root_that_can_run_files() -> None:
    selection = select_temp_root(
        _DEFAULT, None, _FALLBACK, _runnable_only(_DEFAULT, _FALLBACK)
    )

    assert selection.usable_root == _DEFAULT
    assert selection.rejected_roots == ()


def test_select_temp_root_moves_off_a_default_root_that_cannot_run_files() -> None:
    selection = select_temp_root(_DEFAULT, None, _FALLBACK, _runnable_only(_FALLBACK))

    assert selection.usable_root == _FALLBACK
    assert selection.rejected_roots == (_DEFAULT,)


def test_select_temp_root_never_replaces_an_explicit_root_that_cannot_run_files() -> (
    None
):
    selection = select_temp_root(
        _DEFAULT, _EXPLICIT, _FALLBACK, _runnable_only(_DEFAULT, _FALLBACK)
    )

    assert selection.usable_root is None
    assert selection.rejected_roots == (_EXPLICIT,)


def test_select_temp_root_finds_nothing_when_no_candidate_can_run_files() -> None:
    selection = select_temp_root(_DEFAULT, None, _FALLBACK, _runnable_only())

    assert selection.usable_root is None
    assert selection.rejected_roots == (_DEFAULT, _FALLBACK)


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
