"""Where each step of a trial keeps the artifacts the checker reads, in the order harbor ran them."""

import shutil
from pathlib import Path

from imbue.minds_evals.check_run import load_trial_result
from imbue.minds_evals.step_artifacts import read_step_names
from imbue.minds_evals.step_artifacts import resolve_step_artifact_paths
from imbue.minds_evals.testing import DIAGNOSTIC_STEP_NAMES
from imbue.minds_evals.testing import diagnostic_step
from imbue.minds_evals.testing import write_diagnostic_trial_dir
from imbue.minds_evals.testing import write_trial_dir


def _two_step_trial(job_dir: Path, trial_name: str) -> Path:
    return write_diagnostic_trial_dir(
        job_dir,
        trial_name,
        [diagnostic_step(name, step_index=index, step_total=2) for index, name in enumerate(DIAGNOSTIC_STEP_NAMES)],
    )


def test_resolve_step_artifact_paths_takes_the_steps_in_the_order_harbor_ran_them(tmp_path: Path) -> None:
    """`work` ran before `check`, which sorts first; the default step a fact is read from is the last
    one, so name order would read every fact off the wrong step."""
    trial_dir = _two_step_trial(tmp_path / "job", "behaviour__aaaaaaa")

    step_paths = resolve_step_artifact_paths(trial_dir, load_trial_result(trial_dir / "result.json"))

    assert [paths.step_name for paths in step_paths] == ["work", "check"]
    check_paths = step_paths[-1]
    assert check_paths.state_path == trial_dir / "steps" / "check" / "agent" / "state.json"
    assert (
        check_paths.evidence_manifest_path
        == trial_dir / "steps" / "check" / "agent" / "verification" / "manifest.json"
    )
    assert check_paths.reward_details_path == trial_dir / "steps" / "check" / "verifier" / "reward-details.json"
    assert check_paths.derived_dir == trial_dir / "steps" / "check" / "verifier" / "derived"
    assert all(path.is_file() for path in (check_paths.state_path, check_paths.reward_details_path))


def test_read_step_names_falls_back_to_the_step_directories_without_a_result(tmp_path: Path) -> None:
    """Without harbor's record the directories are all there is, so the names come back in name
    order -- which finds every step's artifacts but cannot say which step ran last."""
    trial_dir = _two_step_trial(tmp_path / "job", "behaviour__aaaaaaa")

    assert read_step_names(trial_dir, None) == ("check", "work")


def test_resolve_step_artifact_paths_reads_a_flat_trial_as_one_unnamed_step_at_the_root(tmp_path: Path) -> None:
    trial_dir = write_trial_dir(tmp_path / "job", "todo-app__aaaaaaa")

    (step_paths,) = resolve_step_artifact_paths(trial_dir, load_trial_result(trial_dir / "result.json"))

    assert step_paths.step_name == ""
    assert step_paths.state_path == trial_dir / "agent" / "state.json"
    assert step_paths.reward_details_path == trial_dir / "verifier" / "reward-details.json"
    assert step_paths.derived_dir == trial_dir / "verifier" / "derived"


def test_resolve_step_artifact_paths_finds_what_a_step_that_died_midway_left_at_the_root(tmp_path: Path) -> None:
    """Harbor archives a step's directories only when the step ends, so everything a trial killed
    mid-step recorded is still at the trial root; reading only the step would report each of those
    records as one the instrument never wrote."""
    trial_dir = _two_step_trial(tmp_path / "job", "behaviour__aaaaaaa")
    shutil.move(str(trial_dir / "steps" / "check" / "agent"), str(trial_dir / "agent"))
    shutil.move(str(trial_dir / "steps" / "check" / "verifier"), str(trial_dir / "verifier"))
    (trial_dir / "steps" / "check").rmdir()

    work_paths, check_paths = resolve_step_artifact_paths(trial_dir, load_trial_result(trial_dir / "result.json"))

    assert check_paths.state_path == trial_dir / "agent" / "state.json"
    assert check_paths.evidence_manifest_path.is_file()
    assert check_paths.reward_details_path == trial_dir / "verifier" / "reward-details.json"
    assert work_paths.state_path == trial_dir / "steps" / "work" / "agent" / "state.json"
