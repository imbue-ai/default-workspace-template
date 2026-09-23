import subprocess
import sys
from pathlib import Path

import pytest

from pytest_executable_tmp.plugin import can_run_files_in, select_temp_root

_DEFAULT = Path("/default-root-51873")
_FALLBACK = Path("/fallback-root-51873")
_EXPLICIT = Path("/explicit-root-51873")
# Distinct from 0 (the probe said no) and 1 (the probe raised).
_CHILD_EXIT_WHEN_RUNNABLE = 3


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


def test_can_run_files_in_is_false_where_the_probe_cannot_be_written(
    tmp_path: Path,
) -> None:
    # A zero file-size limit lets the probe's directory be made but not its script
    # written -- a stand-in for a full or read-only disk that works even as root.
    # It binds the whole process, so the probe runs in a child of its own.
    probe_in_a_child = f"""
import resource
import signal
from pathlib import Path

from pytest_executable_tmp.plugin import can_run_files_in

signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(
    resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1])
)
is_runnable = can_run_files_in(Path({str(tmp_path)!r}))
raise SystemExit({_CHILD_EXIT_WHEN_RUNNABLE} if is_runnable else 0)
"""

    result = subprocess.run(
        [sys.executable, "-c", probe_in_a_child],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
