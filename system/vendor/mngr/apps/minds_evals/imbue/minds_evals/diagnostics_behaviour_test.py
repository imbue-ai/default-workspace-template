import json
import shutil
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from harbor.models.task.task import Task

from imbue.minds_evals import ci_matrix
from imbue.minds_evals.data_types import BehaviourHarnessEntry
from imbue.minds_evals.data_types import HarnessName
from imbue.minds_evals.data_types import StepBoxFile
from imbue.minds_evals.data_types import harness_for_lane
from imbue.minds_evals.data_types import harness_id
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.generate import AGENT_TIMEOUT_GRACE_SECONDS
from imbue.minds_evals.generate import STEP_FILES_DIRNAME
from imbue.minds_evals.generate import generate_dataset
from imbue.minds_evals.generate import is_exchange_budget_implausible
from imbue.minds_evals.generate import is_trial_longer_than_the_workspace
from imbue.minds_evals.generate import load_eval_config
from imbue.minds_evals.generate import step_files_box_dir
from imbue.minds_evals.testing import make_local_git_repo

_CONFIGS_DIR: Final[Path] = Path(__file__).parents[2] / "configs"
_BEHAVIOUR_CONFIG_PATH: Final[Path] = _CONFIGS_DIR / "eval-config-diagnostics-behaviour.json"
_BEHAVIOUR_HARNESS_CONFIGS_PATH: Final[Path] = _CONFIGS_DIR / "diagnostics" / "behaviour_harness_configs.json"

_WORK_STEP: Final[str] = "work"
_CHECK_STEP: Final[str] = "check"
_UPLOAD_ID: Final[str] = "diagupload7f3a"


def _behaviour_entry_by_harness() -> dict[HarnessName, BehaviourHarnessEntry]:
    return ci_matrix.load_behaviour_harness_configs(_BEHAVIOUR_HARNESS_CONFIGS_PATH)


def _raw_behaviour_steps() -> list[dict[str, Any]]:
    (persona,) = json.loads(_BEHAVIOUR_CONFIG_PATH.read_text())["personas"]
    return persona["steps"]


def _generate_behaviour_task(tmp_path: Path) -> Path:
    """The behaviour case generated against local stand-ins for mngr and the workspace template, from a
    copy of the config with a copy of every upload it names at the same relative path, since a step's
    files resolve against the config's own directory."""
    mngr_repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config_dir = tmp_path / "configs"
    for step in _raw_behaviour_steps():
        for step_file in step.get("files", []):
            shutil.copytree(_CONFIGS_DIR / step_file["source"], config_dir / step_file["source"])
    config = json.loads(_BEHAVIOUR_CONFIG_PATH.read_text())
    config["dwt_repo"] = str(dwt_repo.repo_dir)
    config_path = config_dir / _BEHAVIOUR_CONFIG_PATH.name
    config_path.write_text(json.dumps(config))
    (task_dir,) = generate_dataset(
        config_path=config_path,
        output_dir=tmp_path / "dataset",
        mngr_repo=str(mngr_repo.repo_dir),
        mngr_ref=None,
        dwt_ref=None,
    )
    return task_dir


def test_every_nightly_harness_has_a_behaviour_harness_config() -> None:
    """A harness made nightly without a cheap diagnostic config would get a behaviour cell with nothing
    to run on, so the loader that decides the matrix refuses the pair of files."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)

    selected = ci_matrix.select_behaviour_harness_configs(
        _behaviour_entry_by_harness(), ci_matrix.select_nightly_harnesses(entries)
    )

    assert [harness_id(harness) for harness, _ in selected] == [
        harness_id(HarnessName.CLAUDE),
        harness_id(HarnessName.PI_CODING),
        harness_id(HarnessName.CODEX),
    ]


def test_the_behaviour_harness_configs_are_each_harness_cheap_model_at_standard_speed() -> None:
    parsed_by_harness = {
        harness: ci_matrix.diagnostic_harness_config_kwargs(entry)
        for harness, entry in _behaviour_entry_by_harness().items()
    }

    assert {
        harness_id(harness): (harness_config.model, harness_config.effort, harness_config.is_fast)
        for harness, harness_config in parsed_by_harness.items()
    } == {
        harness_id(HarnessName.CLAUDE): ("haiku", "medium", False),
        harness_id(HarnessName.PI_CODING): ("openrouter/openai/gpt-5-mini", "medium", False),
        harness_id(HarnessName.CODEX): ("gpt-5.6-luna", "low", False),
    }
    # The loader refuses an entry whose lane runs another harness than the key it is filed under.
    assert [harness_id(harness_for_lane(config.lane)) for harness, config in parsed_by_harness.items()] == [
        harness_id(harness) for harness in parsed_by_harness
    ]


def test_only_the_codex_cell_names_a_pair_it_cannot_run_on() -> None:
    """A cell on a pair whose workspace template offers its lane no pasted-key sign-in has nothing to
    measure, so the matrix leaves it out rather than scheduling a trial that stops at sign-in."""
    unsupported_by_harness = {
        harness_id(harness): entry.unsupported_pairs
        for harness, entry in _behaviour_entry_by_harness().items()
        if entry.unsupported_pairs
    }

    assert unsupported_by_harness == {harness_id(HarnessName.CODEX): ("released",)}


def test_the_behaviour_config_generates_two_steps_whose_prompts_and_upload_the_driver_reads_back(
    tmp_path: Path,
) -> None:
    task_dir = _generate_behaviour_task(tmp_path)

    task = Task(task_dir)
    assert task.config.steps is not None
    assert [step.name for step in task.config.steps] == [_WORK_STEP, _CHECK_STEP]
    raw_work_step, raw_check_step = _raw_behaviour_steps()
    work = parse_case_config((task_dir / "steps" / _WORK_STEP / "instruction.md").read_text())
    check = parse_case_config((task_dir / "steps" / _CHECK_STEP / "instruction.md").read_text())
    assert work.step is not None and check.step is not None
    assert (work.prompts, check.prompts) == (tuple(raw_work_step["prompts"]), tuple(raw_check_step["prompts"]))
    assert (work.step.entries_before, check.step.entries_before) == (0, 1)
    # `work` declares a process block and nothing else, so the collector's evidence phase runs for
    # that block alone; `check` declares no expectations at all.
    assert work.expectations is not None and check.expectations is None
    assert [check_entry.check_id for check_entry in work.expectations.process_checks] == [
        "skill_required_build_app",
        "skill_required_diag_never_invoked_7f3a",
        "skill_forbidden_diag_forbidden_7f3a",
        "worker_launches",
    ]
    assert (
        work.expectations.app_checks,
        work.expectations.http_checks,
        work.expectations.files_checks,
        work.expectations.ui_flow_checks,
        work.expectations.test_commands,
        work.expectations.is_deliverable_bundle_required,
    ) == ((), (), (), (), (), False)
    # Both steps are read by the probe and the feed's tool inputs, so each has its own second record.
    assert (work.step.is_diagnostic_probe_run, check.step.is_diagnostic_probe_run) == (True, True)

    # The upload exists only from the step that introduces it, and the path the check prompt reads is
    # the one it lands at.
    assert work.step.files == ()
    assert not (task_dir / "steps" / _WORK_STEP / "workdir").exists()
    assert check.step.files == (
        StepBoxFile(upload_id=_UPLOAD_ID, box_path="{}/{}".format(step_files_box_dir(_CHECK_STEP), _UPLOAD_ID)),
    )
    staged_marker = task_dir / "steps" / _CHECK_STEP / "workdir" / STEP_FILES_DIRNAME / _UPLOAD_ID / "marker.txt"
    assert staged_marker.read_text() == "DIAG-UPLOAD-7f3a\n"
    assert "data/uploads/{}/marker.txt".format(_UPLOAD_ID) in check.prompts[0]


def test_the_behaviour_config_gives_each_step_half_the_conversation_budget_and_no_reward_floor(
    tmp_path: Path,
) -> None:
    """A floor on the first step would abort the second exactly when the progress reader regressed."""
    task_dir = _generate_behaviour_task(tmp_path)

    task_config = tomllib.loads((task_dir / "task.toml").read_text())
    step_configs = [
        parse_case_config((task_dir / "steps" / name / "instruction.md").read_text())
        for name in (_WORK_STEP, _CHECK_STEP)
    ]
    assert [step_config.timeout_seconds for step_config in step_configs] == [2400.0, 2400.0]
    assert [step_toml["agent"]["timeout_sec"] for step_toml in task_config["steps"]] == [
        2400.0 + 900.0 + AGENT_TIMEOUT_GRACE_SECONDS,
        2400.0 + 900.0 + AGENT_TIMEOUT_GRACE_SECONDS,
    ]
    assert ["min_reward" in step_toml for step_toml in task_config["steps"]] == [False, False]


def test_the_behaviour_config_fits_the_workspace_and_its_conversation_budget() -> None:
    config = load_eval_config(_BEHAVIOUR_CONFIG_PATH)

    (case,) = config.cases
    assert case.steps is not None
    assert not is_trial_longer_than_the_workspace(
        config.timeout_seconds, config.verification_timeout_seconds, len(case.steps)
    )
    assert not is_exchange_budget_implausible(case.prompts, config.timeout_seconds)
