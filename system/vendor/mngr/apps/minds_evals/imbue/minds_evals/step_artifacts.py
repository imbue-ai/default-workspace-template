"""Where each step of one trial keeps the artifacts `check-diagnostics` reads, flat or stepped.

Harbor mounts `<trial>/agent` and `<trial>/verifier` into the box, and for a task with `[[steps]]` it
moves both into `<trial>/steps/<step name>/` at the end of every step. A flat trial is read as one
step with an empty name. Every directory name comes from harbor's own `TrialPaths`.
"""

# CLEANUP: remove this module once PR #959's trial_layout.py is on main, and resolve check_diagnostics'
# per-step paths through it instead (adding the verifier's derived directory to its step artifacts).

from pathlib import Path
from typing import Final

from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection

_STATE_FILENAME: Final[str] = "state.json"
_REWARD_DETAILS_FILENAME: Final[str] = "reward-details.json"
# Where templates/tests/verifier/keep_derived_outputs.py copies the judges' inputs under the verifier's logs.
DERIVED_DIRNAME: Final[str] = "derived"
_EVIDENCE_MANIFEST_RELATIVE_PATH: Final[str] = "{}/{}".format(
    evidence_collection.VERIFICATION_DIRNAME, evidence_collection.MANIFEST_FILENAME
)


class StepArtifactPaths(FrozenModel):
    """Where one step's own copy of each artifact is, whether or not the step ever wrote it."""

    step_name: str = Field(description="The step's name; empty for a flat trial's one step")
    agent_dir: Path = Field(description="The step's own copy of the driver's logs dir")
    verifier_dir: Path = Field(description="The step's own copy of the verifier's logs dir")
    state_path: Path = Field(description="The driver's state.json as that step left it")
    evidence_manifest_path: Path = Field(description="The step's evidence manifest")
    reward_details_path: Path = Field(description="The step's rewardkit breakdown")
    derived_dir: Path = Field(description="The judges' inputs the step's verifier kept")


def read_step_names(trial_dir: Path, result: TrialResult | None) -> tuple[str, ...]:
    """The steps one trial ran, in the order harbor ran them; empty for a flat trial.

    The order is `result.json`'s `step_results`, appended per step harbor starts. Without a readable
    record the step directories are taken in name order, which finds every step's artifacts but
    cannot say which ran last.
    """
    recorded_names = (
        tuple(step_result.step_name for step_result in (result.step_results or ()) if step_result.step_name)
        if result is not None
        else ()
    )
    if recorded_names:
        return recorded_names
    steps_dir = TrialPaths(trial_dir=trial_dir).steps_dir
    if not steps_dir.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in steps_dir.iterdir() if entry.is_dir()))


@pure
def _step_artifact_paths(step_name: str, agent_dir: Path, verifier_dir: Path) -> StepArtifactPaths:
    return StepArtifactPaths(
        step_name=step_name,
        agent_dir=agent_dir,
        verifier_dir=verifier_dir,
        state_path=agent_dir / _STATE_FILENAME,
        evidence_manifest_path=agent_dir / _EVIDENCE_MANIFEST_RELATIVE_PATH,
        reward_details_path=verifier_dir / _REWARD_DETAILS_FILENAME,
        derived_dir=verifier_dir / DERIVED_DIRNAME,
    )


def _is_step_archived(paths: TrialPaths, step_name: str) -> bool:
    """Whether harbor moved the step's own directories under `steps/`.

    It archives them only once the step ends, so a trial that died mid-step keeps that step's whole
    agent and verifier output at the trial root. Reading only the step directory would report every
    record it left there as one the instrument never wrote.
    """
    return paths.step_agent_dir(step_name).is_dir() or not paths.agent_dir.is_dir()


def resolve_step_artifact_paths(trial_dir: Path, result: TrialResult | None) -> tuple[StepArtifactPaths, ...]:
    """Every step's artifact paths, in step order; one unnamed step at the trial root for a flat trial."""
    paths = TrialPaths(trial_dir=trial_dir)
    step_names = read_step_names(trial_dir, result)
    if not step_names:
        return (_step_artifact_paths("", paths.agent_dir, paths.verifier_dir),)
    return tuple(
        _step_artifact_paths(step_name, *_step_directories(paths, step_name, index == len(step_names) - 1))
        for index, step_name in enumerate(step_names)
    )


@pure
def _step_directories(paths: TrialPaths, step_name: str, is_last: bool) -> tuple[Path, Path]:
    """Where one step's agent and verifier output is: under `steps/`, or at the trial root for a
    last step harbor never got to archive."""
    if is_last and not _is_step_archived(paths, step_name):
        return paths.agent_dir, paths.verifier_dir
    return paths.step_agent_dir(step_name), paths.step_verifier_dir(step_name)
