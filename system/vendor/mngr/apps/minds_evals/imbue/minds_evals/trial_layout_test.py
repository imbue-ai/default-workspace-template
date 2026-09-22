import shutil
from pathlib import Path

from imbue.minds_evals.testing import DIAGNOSTIC_STEP_NAMES
from imbue.minds_evals.testing import StepFixture
from imbue.minds_evals.testing import diagnostic_step
from imbue.minds_evals.testing import expected_modal_environment_name
from imbue.minds_evals.testing import trial_usage_payload
from imbue.minds_evals.testing import write_diagnostic_trial_dir
from imbue.minds_evals.testing import write_stepped_trial_dir
from imbue.minds_evals.testing import write_trial_dir
from imbue.minds_evals.trial_layout import read_step_names
from imbue.minds_evals.trial_layout import resolve_trial_layout


def _two_step_diagnostic_trial(job_dir: Path, trial_name: str) -> Path:
    return write_diagnostic_trial_dir(
        job_dir,
        trial_name,
        [diagnostic_step(name, step_index=index, step_total=2) for index, name in enumerate(DIAGNOSTIC_STEP_NAMES)],
    )


def test_a_stepped_trial_keeps_every_artifact_under_the_step_harbor_moved_it_into(tmp_path: Path) -> None:
    """Where this module's whole job comes from, pinned against harbor itself rather than against a
    description of it: the fixture writes each step into the trial-root mount targets and archives it
    with harbor's own `ArtifactHandler.move_dir_contents` and `TrialPaths.cleanup_empty_mount_dirs`,
    so these paths are where a real stepped trial's artifacts end up. Nothing is left at the trial
    root but harbor's own result.json -- which is why a reader that looks only there finds a trial
    that never got past setup."""
    trial_dir = write_stepped_trial_dir(
        tmp_path / "nightly-run",
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage"),
            StepFixture(name="build"),
            StepFixture(name="amend", usage=trial_usage_payload()),
        ),
    )

    assert sorted(path.name for path in trial_dir.iterdir()) == ["result.json", "steps"]
    assert sorted(path.name for path in (trial_dir / "steps").iterdir()) == ["amend", "build", "triage"]
    assert (trial_dir / "steps" / "amend" / "agent" / "state.json").is_file()
    assert (trial_dir / "steps" / "amend" / "agent" / "usage.json").is_file()
    assert (trial_dir / "steps" / "amend" / "agent" / "verification" / "manifest.json").is_file()
    assert (trial_dir / "steps" / "amend" / "verifier" / "reward-details.json").is_file()


def test_resolve_trial_layout_reads_a_flat_trial_from_the_trial_root(tmp_path: Path) -> None:
    """A trial harbor ran flat resolves to exactly the trial-root paths, under no step name at all:
    every reader of a job directory goes through this resolver, so a flat trial's artifacts have to
    come back from the directories harbor mounts."""
    trial_dir = write_trial_dir(tmp_path / "nightly-run", "todo-app__aaaaaaa", usage=trial_usage_payload())

    layout = resolve_trial_layout(trial_dir)

    assert layout.step_names == ()
    assert layout.result_path == trial_dir / "result.json"
    assert layout.state_path == trial_dir / "agent" / "state.json"
    assert layout.usage_path == trial_dir / "agent" / "usage.json"
    (step,) = layout.steps
    assert step.step_name == ""
    assert step.state_path == trial_dir / "agent" / "state.json"
    assert step.evidence_manifest_path == trial_dir / "agent" / "verification" / "manifest.json"
    assert step.reward_details_path == trial_dir / "verifier" / "reward-details.json"
    assert step.derived_dir == trial_dir / "verifier" / "derived"


def test_resolve_trial_layout_reads_a_stepped_trial_from_its_steps(tmp_path: Path) -> None:
    trial_dir = write_stepped_trial_dir(
        tmp_path / "nightly-run",
        "todo-app__aaaaaaa",
        (StepFixture(name="triage"), StepFixture(name="build", usage=trial_usage_payload())),
    )

    layout = resolve_trial_layout(trial_dir)

    assert layout.step_names == ("triage", "build")
    assert layout.state_path == trial_dir / "steps" / "build" / "agent" / "state.json"
    assert layout.usage_path == trial_dir / "steps" / "build" / "agent" / "usage.json"
    assert [step.step_name for step in layout.steps] == ["triage", "build"]
    assert [step.evidence_manifest_path.parent.parent.parent.name for step in layout.steps] == ["triage", "build"]
    assert [step.reward_details_path.parent.parent.name for step in layout.steps] == ["triage", "build"]


def test_resolve_trial_layout_takes_the_steps_in_the_order_harbor_ran_them(tmp_path: Path) -> None:
    """`work` ran before `check`, which sorts first; the default step a fact is read from is the last
    one, so name order would read every fact off the wrong step."""
    trial_dir = _two_step_diagnostic_trial(tmp_path / "job", "behaviour__aaaaaaa")

    layout = resolve_trial_layout(trial_dir)

    assert [step.step_name for step in layout.steps] == ["work", "check"]
    check_step = layout.steps[-1]
    assert check_step.state_path == trial_dir / "steps" / "check" / "agent" / "state.json"
    assert check_step.evidence_manifest_path == (
        trial_dir / "steps" / "check" / "agent" / "verification" / "manifest.json"
    )
    assert check_step.reward_details_path == trial_dir / "steps" / "check" / "verifier" / "reward-details.json"
    assert check_step.derived_dir == trial_dir / "steps" / "check" / "verifier" / "derived"
    assert all(path.is_file() for path in (check_step.state_path, check_step.reward_details_path))


def test_read_step_names_falls_back_to_the_step_directories_without_a_result(tmp_path: Path) -> None:
    """Without harbor's record the directories are all there is, so the names come back in name
    order -- which finds every step's artifacts but cannot say which step ran last."""
    trial_dir = _two_step_diagnostic_trial(tmp_path / "job", "behaviour__aaaaaaa")
    (trial_dir / "result.json").unlink()

    assert read_step_names(trial_dir) == ("check", "work")


def test_resolve_trial_layout_reads_the_copy_a_dying_step_left_at_the_trial_root(tmp_path: Path) -> None:
    """A step's outputs reach its step directory only when the step ends, so a trial that died mid-step
    keeps its newest state and cost account at the trial root, beside the empty step directory harbor
    created before the step began. Read from the steps alone, such a trial looks like one that never
    wrote a state file -- and the Modal environment its state file names would be left up.

    Its step-local records are at the root too, and an empty step directory standing there is what
    they must not be looked for in: an unfound evidence manifest reads as a step with nothing
    unmeasured, which is the one verdict the run gate exists to deny.
    """
    trial_dir = write_stepped_trial_dir(
        tmp_path / "nightly-run",
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage"),
            StepFixture(name="build", usage=trial_usage_payload(), is_archived=False),
        ),
    )

    layout = resolve_trial_layout(trial_dir)

    assert layout.step_names == ("triage", "build")
    assert layout.state_path == trial_dir / "agent" / "state.json"
    assert layout.usage_path == trial_dir / "agent" / "usage.json"
    assert (trial_dir / "steps" / "build" / "agent").is_dir()
    dying_step = layout.steps[-1]
    assert dying_step.evidence_manifest_path == trial_dir / "agent" / "verification" / "manifest.json"
    assert dying_step.reward_details_path == trial_dir / "verifier" / "reward-details.json"
    assert all(path.is_file() for path in (dying_step.evidence_manifest_path, dying_step.reward_details_path))


def test_resolve_trial_layout_finds_what_a_step_that_died_midway_left_at_the_root(tmp_path: Path) -> None:
    """Harbor archives a step's directories only when the step ends, so everything a trial killed
    mid-step recorded is still at the trial root; reading only the step would report each of those
    per-step records as one the instrument never wrote."""
    trial_dir = _two_step_diagnostic_trial(tmp_path / "job", "behaviour__aaaaaaa")
    shutil.move(str(trial_dir / "steps" / "check" / "agent"), str(trial_dir / "agent"))
    shutil.move(str(trial_dir / "steps" / "check" / "verifier"), str(trial_dir / "verifier"))
    (trial_dir / "steps" / "check").rmdir()

    work_step, check_step = resolve_trial_layout(trial_dir).steps

    assert check_step.state_path == trial_dir / "agent" / "state.json"
    assert check_step.evidence_manifest_path.is_file()
    assert check_step.reward_details_path == trial_dir / "verifier" / "reward-details.json"
    assert work_step.state_path == trial_dir / "steps" / "work" / "agent" / "state.json"


def test_resolve_trial_layout_falls_back_to_the_last_step_that_wrote_a_state_file(tmp_path: Path) -> None:
    """A step killed before it synced anything out wrote no state file anywhere, and the record the
    step before it left is still the trial's own -- the environment it names is the one to delete."""
    trial_dir = write_stepped_trial_dir(
        tmp_path / "nightly-run",
        "todo-app__aaaaaaa",
        (StepFixture(name="triage"), StepFixture(name="build", is_state_written=False)),
    )

    layout = resolve_trial_layout(trial_dir)

    triage_state_path = trial_dir / "steps" / "triage" / "agent" / "state.json"
    assert layout.state_path == triage_state_path
    assert expected_modal_environment_name("todo-app__aaaaaaa") in triage_state_path.read_text()
