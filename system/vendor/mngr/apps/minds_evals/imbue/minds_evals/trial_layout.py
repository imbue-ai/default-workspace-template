"""Where one trial's artifacts are, whether harbor ran the trial flat or as steps.

Harbor mounts `<trial>/agent` and `<trial>/verifier` into the box, and for a task with `[[steps]]` it
moves the contents of both into `<trial>/steps/<step name>/` at the end of every step and removes the
emptied root directories (`MultiStepTrial._archive_step_outputs` and
`TrialPaths.cleanup_empty_mount_dirs`). A reader that looks only at the trial root therefore finds
nothing at all on a stepped trial: no state file, which reads as a trial that never got past setup;
no cost account, so every spend cell is blank; and no evidence manifest, which reads -- silently --
as a trial with nothing unmeasured. The run gate, the diagnostics checker and the environment cleanup
all resolve their paths through here, so none of them can grow that blind spot on its own.

Every directory name is harbor's own `TrialPaths`, not a copy of it, so a layout harbor moves arrives
here as a moved path rather than as a read that quietly finds nothing.

What a step holds is not uniform, so each artifact is placed by what it means. The driver's
`state.json` and `usage.json` accumulate across the steps -- one conversation and one proxy serve
every step -- so the last step that wrote one describes the whole trial. Everything else a step
leaves under its agent and verifier directories is produced fresh per step, so every step's copy is
its own claim about that step, and the per-step layout says where each step's copy is.
"""

import json
from pathlib import Path
from typing import Any
from typing import Final

from harbor.models.trial.paths import TrialPaths
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.errors import JobReadError

# The driver's own files inside harbor's agent dir. Spelled out rather than taken from
# driver.STATE_FILENAME: importing the driver would pull harbor's agent base into the CLI's graph for
# one word, and this path drifting is loud anyway -- a trial whose state.json is not found is charged
# for never writing one.
_STATE_FILENAME: Final[str] = "state.json"
# Absent for every trial that never got as far as writing one, and for every oracle trial, which runs
# no models of its own.
_USAGE_FILENAME: Final[str] = "usage.json"
# rewardkit's per-criterion breakdown, written into the verifier's own logs dir by
# templates/tests/verifier/finalize.py.
_REWARD_DETAILS_FILENAME: Final[str] = "reward-details.json"
# Where templates/tests/verifier/keep_derived_outputs.py copies the judges' inputs under the verifier's logs.
DERIVED_DIRNAME: Final[str] = "derived"
# Composed from the collector's own constants, the way generate.py composes it. Drift here would be
# SILENT: an unfound manifest is the "a trial that never reached a workspace writes none" case, which
# is deliberately quiet, so every trial would report nothing unmeasured -- the one verdict the run
# gate exists to deny.
_EVIDENCE_MANIFEST_RELATIVE_PATH: Final[str] = "{}/{}".format(
    evidence_collection.VERIFICATION_DIRNAME, evidence_collection.MANIFEST_FILENAME
)

# What the step of a flat trial is called, which is nothing: it has one copy of every artifact and no
# step to qualify it with. Harbor requires a non-empty name for every declared step, so no stepped
# trial can collide with it.
_NO_STEP: Final[str] = ""


def load_json_object(path: Path) -> dict[str, Any]:
    """Raises JobReadError if the file is absent, is not decodable, or is not a JSON object."""
    try:
        # Decoded explicitly rather than via read_text() so that a file truncated mid-write, ending
        # on a partial multi-byte sequence, lands in this handler instead of raising a bare
        # UnicodeDecodeError -- which is a ValueError, not an OSError.
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise JobReadError("cannot read {}: {}".format(path, exc)) from exc
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise JobReadError("{} is not valid JSON: {}".format(path, exc)) from exc
    if not isinstance(parsed, dict):
        raise JobReadError("{} is a {}, not a JSON object".format(path, type(parsed).__name__))
    return parsed


def load_optional_json_object(path: Path) -> dict[str, Any] | None:
    """The file's contents, or None when it was never written. An unreadable file still raises:
    absent and corrupt are different claims and only the first one is expected."""
    if not path.is_file():
        return None
    return load_json_object(path)


class StepLayout(FrozenModel):
    """Where one step's own copy of each per-step artifact is, whether or not the step ever wrote it."""

    step_name: str = Field(description="The step's name; empty for a flat trial's one step")
    agent_dir: Path = Field(description="The step's own copy of the driver's logs dir")
    verifier_dir: Path = Field(description="The step's own copy of the verifier's logs dir")
    state_path: Path = Field(description="The driver's state.json as that step left it")
    evidence_manifest_path: Path = Field(description="The step's evidence manifest")
    reward_details_path: Path = Field(description="The step's rewardkit breakdown")
    derived_dir: Path = Field(description="The judges' inputs the step's verifier kept")


class TrialLayout(FrozenModel):
    """Which file each of one trial's artifacts is in, resolved against what the trial left on disk."""

    trial_dir: Path = Field(description="The trial directory every path below is under")
    step_names: tuple[str, ...] = Field(
        description="The steps harbor ran, in the order it ran them; empty for a flat trial"
    )
    result_path: Path = Field(description="Harbor's own record of the trial, which is at the trial root always")
    state_path: Path | None = Field(
        description="The driver's cumulative state record; None when no step of the trial wrote one"
    )
    usage_path: Path | None = Field(
        description="The driver's cumulative cost account; None when no step of the trial wrote one"
    )
    steps: tuple[StepLayout, ...] = Field(
        description="Every step's own artifact paths, in step order; one unnamed step for a flat trial"
    )


@pure
def _recorded_step_names(result: dict[str, Any] | None) -> tuple[str, ...]:
    """The steps named in harbor's own record of the trial, in the order it ran them.

    Read without a schema, beside the validated read the run gate does of the same file: this one
    only has to place the artifacts, and a result.json whose schema moved still names its steps.
    """
    raw_step_results = (result or {}).get("step_results")
    if not isinstance(raw_step_results, list):
        return ()
    return tuple(
        str(step_result.get("step_name") or "")
        for step_result in raw_step_results
        if isinstance(step_result, dict) and step_result.get("step_name")
    )


def read_step_names(trial_dir: Path) -> tuple[str, ...]:
    """The steps one trial ran, in harbor's own order; empty for a trial that ran no steps at all.

    The order is taken from `result.json`'s `step_results`, which `MultiStepTrial._run` appends to as
    it iterates the task's `[[steps]]`. That is the only statement of the order inside a job
    directory -- the task config, which is the other one, is not there -- and the step directory
    names carry none of their own, since a step called `final` sorts before one called `handoff`.

    A trial whose result.json is absent or cannot be read falls back to the directory names, which
    orders nothing but still finds the artifacts: such a trial fails the run gate on its missing
    record anyway, and the fallback is what recovers its cost account and the environment it leaked.
    """
    paths = TrialPaths(trial_dir=trial_dir)
    if not paths.steps_dir.is_dir():
        return ()
    try:
        recorded_names = _recorded_step_names(load_optional_json_object(paths.result_path))
    except JobReadError as exc:
        # Absorbed rather than raised, unlike the gate's own read of this file: the cleanup pass
        # reads the layout of a trial truncated by the very crash being cleaned up after, and
        # refusing it there would leak that trial's environment.
        logger.warning("{} cannot be read, so {} is read in directory order ({})", paths.result_path, trial_dir, exc)
        recorded_names = ()
    if recorded_names:
        return recorded_names
    return tuple(sorted(entry.name for entry in paths.steps_dir.iterdir() if entry.is_dir()))


@pure
def _step_layout(step_name: str, agent_dir: Path, verifier_dir: Path) -> StepLayout:
    return StepLayout(
        step_name=step_name,
        agent_dir=agent_dir,
        verifier_dir=verifier_dir,
        state_path=agent_dir / _STATE_FILENAME,
        evidence_manifest_path=agent_dir / _EVIDENCE_MANIFEST_RELATIVE_PATH,
        reward_details_path=verifier_dir / _REWARD_DETAILS_FILENAME,
        derived_dir=verifier_dir / DERIVED_DIRNAME,
    )


def _is_directory_filled(directory: Path) -> bool:
    """Whether a directory holds anything at all."""
    return directory.is_dir() and any(directory.iterdir())


def _is_step_archived(paths: TrialPaths, step_name: str) -> bool:
    """Whether harbor moved the step's own output under `steps/`.

    Harbor creates both step directories before the step runs (`MultiStepTrial._create_step_dirs`)
    and moves the output into them only once the step ends, so their existence says nothing: a trial
    that died mid-step has empty ones beside that step's whole agent and verifier output at the trial
    root, and reading the step directory there would report every record it left as one the
    instrument never wrote. What the move leaves behind is content, so content is what is looked for
    -- in either directory, since a step can end with one of them empty. A trial whose root mount
    dirs are gone was archived and then cleaned up, which is the one archived state that leaves no
    content to find.
    """
    return (
        _is_directory_filled(paths.step_agent_dir(step_name))
        or _is_directory_filled(paths.step_verifier_dir(step_name))
        or not paths.agent_dir.is_dir()
    )


def _step_directories(paths: TrialPaths, step_name: str, is_last: bool) -> tuple[Path, Path]:
    """Where one step's agent and verifier output is: under `steps/`, or at the trial root for a
    last step harbor never got to archive."""
    if is_last and not _is_step_archived(paths, step_name):
        return paths.agent_dir, paths.verifier_dir
    return paths.step_agent_dir(step_name), paths.step_verifier_dir(step_name)


def _last_written_path(candidates: tuple[Path, ...]) -> Path | None:
    """The last of an artifact's copies that is on disk, or None when none of them is.

    For a cumulative artifact the last copy is the whole record and every earlier one is the same
    record partway through, so the copies before the last are not read. An earlier step's copy is
    reached only where the step that ran last wrote nothing at all, which is a step harbor recorded an
    exception for -- the gate reports that on the step result, so no reading of a stale state file can
    pass such a trial off as one that finished.
    """
    written = [path for path in candidates if path.is_file()]
    return written[-1] if written else None


@pure
def _cumulative_candidates(paths: TrialPaths, agent_dirs: tuple[Path, ...], filename: str) -> tuple[Path, ...]:
    """Every copy of one cumulative artifact a trial can have left behind, oldest first.

    The trial root comes last because a step's agent dir is archived into `steps/<name>/` only at the
    END of the step, while harbor creates that step directory before the step begins. A trial that
    died mid-step -- an exception out of a phase `MultiStepTrial._run_step` does not guard, or the
    whole job being killed -- therefore keeps its newest copy at the root, beside an empty step
    directory. Reading the steps alone would report that trial as one that never wrote a state file
    and leave the Modal environment it names standing. Anything at the root is the newest copy there
    is, since a successful archive empties it.
    """
    step_copies = tuple(agent_dir / filename for agent_dir in agent_dirs)
    root_copy = paths.agent_dir / filename
    # Already among the candidates for a flat trial, and for a stepped trial whose last step was
    # never archived, since both resolve that step's agent dir to the root one.
    return step_copies if root_copy in step_copies else (*step_copies, root_copy)


def resolve_trial_layout(trial_dir: Path) -> TrialLayout:
    """Where to read each artifact of one trial, for a trial harbor ran flat or as steps.

    A flat trial resolves to exactly the trial-root paths, under one step with no name.
    """
    paths = TrialPaths(trial_dir=trial_dir)
    step_names = read_step_names(trial_dir)
    if step_names:
        steps = tuple(
            _step_layout(step_name, *_step_directories(paths, step_name, index == len(step_names) - 1))
            for index, step_name in enumerate(step_names)
        )
    else:
        steps = (_step_layout(_NO_STEP, paths.agent_dir, paths.verifier_dir),)
    agent_dirs = tuple(step.agent_dir for step in steps)
    return TrialLayout(
        trial_dir=trial_dir,
        step_names=step_names,
        result_path=paths.result_path,
        state_path=_last_written_path(_cumulative_candidates(paths, agent_dirs, _STATE_FILENAME)),
        usage_path=_last_written_path(_cumulative_candidates(paths, agent_dirs, _USAGE_FILENAME)),
        steps=steps,
    )
