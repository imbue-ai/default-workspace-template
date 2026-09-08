import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from harbor.models.task.config import MultiStepRewardStrategy
from harbor.models.task.task import Task
from inline_snapshot import snapshot
from pydantic import ValidationError
from rewardkit.runner import discover

from imbue.minds_evals.data_types import ComposedRewardFloor
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DEFAULT_AVG_WORD_COUNT_BASELINE
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.data_types import DEFAULT_MAX_EXCHANGES
from imbue.minds_evals.data_types import DEFAULT_VERIFICATION_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import EvalConfig
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import MAX_EXCHANGES_CAP
from imbue.minds_evals.data_types import PerDimensionRewardFloors
from imbue.minds_evals.data_types import RewardDimension
from imbue.minds_evals.data_types import RewardFloor
from imbue.minds_evals.data_types import RewardStrategy
from imbue.minds_evals.data_types import StepFile
from imbue.minds_evals.data_types import StepMinReward
from imbue.minds_evals.data_types import is_final_step
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.errors import GitSourceError
from imbue.minds_evals.generate import AGENT_TIMEOUT_GRACE_SECONDS
from imbue.minds_evals.generate import STEP_FILES_DIRNAME
from imbue.minds_evals.generate import TYPICAL_EXCHANGE_SECONDS
from imbue.minds_evals.generate import VERIFIER_CRITERIA_DIRNAME
from imbue.minds_evals.generate import VERIFIER_TIMEOUT_SECONDS
from imbue.minds_evals.generate import derive_case_id
from imbue.minds_evals.generate import generate_dataset
from imbue.minds_evals.generate import is_exchange_budget_implausible
from imbue.minds_evals.generate import is_trial_longer_than_the_workspace
from imbue.minds_evals.generate import load_eval_config
from imbue.minds_evals.generate import render_min_reward_toml
from imbue.minds_evals.generate import render_oracle_trajectory_json
from imbue.minds_evals.generate import render_prompt_entry_prose
from imbue.minds_evals.generate import resolve_remote_tip
from imbue.minds_evals.generate import step_files_box_dir
from imbue.minds_evals.generate import worst_case_exchange_count
from imbue.minds_evals.minds_bridge import EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS
from imbue.minds_evals.testing import make_local_git_repo


def _write_config(tmp_path: Path, config: dict[str, object]) -> Path:
    config_path = tmp_path / "eval-config.json"
    config_path.write_text(json.dumps(config))
    return config_path


def _valid_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "mngr_branch": "main",
        "timeout_seconds": 1800,
        "personas": [
            {
                "id": "todo-app",
                "persona": "Non-technical founder.",
                "prompts": ["Build me a to-do app", "Sounds good.", DECIDE_SENTINEL],
            },
            {"prompts": ["hi what can you do", DECIDE_SENTINEL]},
        ],
    }
    config.update(overrides)
    return config


def test_load_eval_config_parses_cases_and_defaults(tmp_path: Path) -> None:
    config = load_eval_config(_write_config(tmp_path, _valid_config()))

    assert config.mngr_branch == "main"
    assert config.dwt_repo == DEFAULT_DWT_REPO
    assert config.timeout_seconds == 1800.0
    assert config.avg_word_count_baseline == DEFAULT_AVG_WORD_COUNT_BASELINE
    assert [case.case_id for case in config.cases] == ["todo-app", "case-2"]
    assert config.cases[1].persona == ""


def test_load_eval_config_rejects_missing_mngr_branch(tmp_path: Path) -> None:
    with pytest.raises(EvalConfigError, match="mngr_branch"):
        load_eval_config(_write_config(tmp_path, {"personas": [{"prompts": ["hi"]}]}))


def test_load_eval_config_rejects_decide_sentinel_as_first_prompt(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "bad", "prompts": [DECIDE_SENTINEL, "hi"]}])

    with pytest.raises(EvalConfigError, match="first prompt"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "dup", "prompts": ["a"]}, {"id": "dup", "prompts": ["b"]}])

    with pytest.raises(EvalConfigError, match="duplicate case id"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_empty_prompt(tmp_path: Path) -> None:
    """Named by position, like every other bad-prompt error: a case can have several."""
    config = _valid_config(personas=[{"id": "empty", "prompts": ["hi", "  "]}])

    with pytest.raises(EvalConfigError, match="prompt 2: a prompt must not be empty"):
        load_eval_config(_write_config(tmp_path, config))


@pytest.mark.parametrize(("raw_prompt", "type_name"), [(None, "NoneType"), (True, "bool"), (3, "int"), ([], "list")])
def test_load_eval_config_rejects_a_prompt_that_is_neither_a_message_nor_a_goal(
    tmp_path: Path, raw_prompt: object, type_name: str
) -> None:
    """Coercing these to `str` would send a real agent the literal message "None" or "3", so the
    authoring mistake would surface as a wasted trial rather than as a failed generation."""
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", raw_prompt]}])

    with pytest.raises(EvalConfigError, match="prompt 2: a prompt must be a message string or a goal object"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_parses_a_goal_entry_and_defaults_its_budget(tmp_path: Path) -> None:
    config = _valid_config(
        personas=[{"id": "goal", "prompts": ["Build me a to-do app", {"goal": "  See it running  "}]}]
    )

    parsed = load_eval_config(_write_config(tmp_path, config))

    entry = parsed.cases[0].prompts[1]
    assert isinstance(entry, GoalEntry)
    assert entry.goal == "See it running"
    assert entry.max_exchanges == DEFAULT_MAX_EXCHANGES


def test_load_eval_config_rejects_a_goal_entry_as_the_first_prompt(tmp_path: Path) -> None:
    """The opening ask commissions the work and is what the oracle sends verbatim, so it stays a
    literal string."""
    config = _valid_config(personas=[{"id": "bad", "prompts": [{"goal": "See it running"}, "thanks"]}])

    with pytest.raises(EvalConfigError, match="first prompt must be a literal message"):
        load_eval_config(_write_config(tmp_path, config))


@pytest.mark.parametrize("raw_goal", [123, True, ["a", "b"], {"x": 1}, None])
def test_load_eval_config_rejects_a_goal_that_is_not_a_string(tmp_path: Path, raw_goal: object) -> None:
    """Coercing these to `str` would make "123" or "['a', 'b']" the thing the client is told to hold
    out for, and the bad config would steer a real conversation instead of failing generation."""
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"goal": raw_goal}]}])

    with pytest.raises(EvalConfigError, match="prompt 2: a goal entry needs a non-empty 'goal' string"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_a_goal_entry_with_no_goal_at_all(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"max_exchanges": 2}]}])

    with pytest.raises(EvalConfigError, match="non-empty 'goal' string"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_an_empty_goal(tmp_path: Path) -> None:
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"goal": "   "}]}])

    with pytest.raises(EvalConfigError, match="non-empty 'goal'"):
        load_eval_config(_write_config(tmp_path, config))


@pytest.mark.parametrize(
    ("budget", "expected_message"),
    [
        (0, "greater than or equal to 1"),
        (-1, "greater than or equal to 1"),
        (MAX_EXCHANGES_CAP + 1, "less than or equal to {}".format(MAX_EXCHANGES_CAP)),
    ],
)
def test_load_eval_config_rejects_a_budget_outside_the_cap(tmp_path: Path, budget: int, expected_message: str) -> None:
    """Each exchange is a full agent turn in a real workspace, so an implausible budget must fail
    generation rather than surface as a trial that runs for hours."""
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"goal": "g", "max_exchanges": budget}]}])

    with pytest.raises(EvalConfigError, match="prompt 2: max_exchanges: Input should be {}".format(expected_message)):
        load_eval_config(_write_config(tmp_path, config))


@pytest.mark.parametrize("budget", ["3", 2.5, True, None])
def test_load_eval_config_rejects_a_non_integer_budget(tmp_path: Path, budget: object) -> None:
    """A budget is read strictly: a quoted number or a bool in the config is an authoring mistake,
    not something to coerce into a cost commitment."""
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"goal": "g", "max_exchanges": budget}]}])

    with pytest.raises(EvalConfigError, match="max_exchanges: Input should be a valid integer"):
        load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_rejects_unknown_goal_entry_keys(tmp_path: Path) -> None:
    """A misspelled key would silently take the default budget or drop a stop condition."""
    config = _valid_config(personas=[{"id": "bad", "prompts": ["Build it", {"goal": "g", "max_exchange": 4}]}])

    with pytest.raises(EvalConfigError, match="max_exchange: Extra inputs are not permitted"):
        load_eval_config(_write_config(tmp_path, config))


def test_goal_entry_bounds_itself_on_the_model_the_driver_re_validates(tmp_path: Path) -> None:
    """The generator is not the only reader: the driver re-validates the case config out of
    instruction.md at trial time, so both bounds have to hold there too -- an entry with no goal
    would have a model hold out for nothing."""
    with pytest.raises(ValidationError, match="less than or equal to {}".format(MAX_EXCHANGES_CAP)):
        GoalEntry(goal="g", max_exchanges=MAX_EXCHANGES_CAP + 1)
    with pytest.raises(ValidationError, match="at least 1 character"):
        GoalEntry(goal="")
    assert GoalEntry(goal="g").max_exchanges == DEFAULT_MAX_EXCHANGES


def test_worst_case_exchange_count_sums_every_entrys_ceiling() -> None:
    prompts = ("Build it", DECIDE_SENTINEL, GoalEntry(goal="g", max_exchanges=5))

    assert worst_case_exchange_count(prompts) == 7


def test_is_exchange_budget_implausible_measures_the_worst_case_against_the_wall_clock() -> None:
    """A goal entry multiplies what a case can spend, so a budget sized for one message per entry
    silently becomes a timed-out trial. The warning this decides is the only thing that says so."""
    prompts = ("Build it", GoalEntry(goal="g", max_exchanges=8))
    needed_seconds = 9 * TYPICAL_EXCHANGE_SECONDS

    assert is_exchange_budget_implausible(prompts, needed_seconds - 1)
    assert not is_exchange_budget_implausible(prompts, needed_seconds)
    # The same opening ask alone fits a budget a ninth the size: only the goal entry's ceiling moved.
    assert not is_exchange_budget_implausible(("Build it",), TYPICAL_EXCHANGE_SECONDS)


def test_render_prompt_entry_prose_marks_a_goal_entry_as_a_budgeted_conversation() -> None:
    """A reader of instruction.md must never mistake goal text for a message sent verbatim."""
    prose = render_prompt_entry_prose(GoalEntry(goal="See it running", max_exchanges=4))

    assert prose == snapshot(
        "(goal, up to 4 exchange(s)) the client keeps the conversation going until it is satisfied that: See it running"
    )
    assert render_prompt_entry_prose("Build it") == "Build it"
    assert render_prompt_entry_prose(DECIDE_SENTINEL) == "`{}`".format(DECIDE_SENTINEL)


def test_generate_dataset_renders_a_goal_entry_into_both_case_copies_and_the_oracle(tmp_path: Path) -> None:
    mngr_repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config = _valid_config(
        dwt_repo=str(dwt_repo.repo_dir),
        personas=[
            {
                "id": "goal-case",
                "prompts": ["Build me a to-do app", {"goal": "See it running in the browser", "max_exchanges": 4}],
            }
        ],
    )

    task_dirs = generate_dataset(
        config_path=_write_config(tmp_path, config),
        output_dir=tmp_path / "dataset",
        mngr_repo=str(mngr_repo.repo_dir),
    )

    task_dir = task_dirs[0]
    # The case config rides the same transport in both places, and both copies must agree.
    case_json = json.loads((task_dir / "tests" / "case.json").read_text())
    assert case_json["prompts"][1] == {"goal": "See it running in the browser", "max_exchanges": 4}
    assert parse_case_config((task_dir / "instruction.md").read_text()).prompts[1] == GoalEntry(
        goal="See it running in the browser", max_exchanges=4
    )
    # The instruction's prose renders the goal as a budgeted conversation, not as a message.
    assert "up to 4 exchange(s)" in (task_dir / "instruction.md").read_text()

    # The oracle sends the goal as one literal message and records the entry as satisfied, so its
    # fabricated state reconciles with the trajectory the structural gates read.
    solve_text = (task_dir / "solution" / "solve.sh").read_text()
    assert '"message": "See it running in the browser"' in solve_text
    assert '"outcome": "satisfied"' in solve_text
    assert '"waits_done": 2' in solve_text


def test_derive_case_id_prefers_explicit_id_and_falls_back_to_position() -> None:
    assert derive_case_id({"id": "explicit"}, 0) == "explicit"
    assert derive_case_id({}, 2) == "case-3"


def test_resolve_remote_tip_returns_the_branch_tip(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=2)

    assert resolve_remote_tip(str(repo.repo_dir), "main") == repo.commit_shas[-1]


def test_resolve_remote_tip_raises_for_missing_branch(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)

    with pytest.raises(GitSourceError, match="not found"):
        resolve_remote_tip(str(repo.repo_dir), "no-such-branch-8471")


def _dir_content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_generate_dataset_writes_complete_byte_identical_tasks(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=2)
    config_path = _write_config(tmp_path, _valid_config(dwt_repo=str(dwt_repo.repo_dir)))
    output_dir = tmp_path / "dataset"

    task_dirs = generate_dataset(config_path=config_path, output_dir=output_dir, mngr_repo=str(repo.repo_dir))

    assert [task_dir.name for task_dir in task_dirs] == ["todo-app", "case-2"]
    expected_sha = repo.commit_shas[-1]
    expected_dwt_sha = dwt_repo.commit_shas[-1]

    for task_dir in task_dirs:
        task_config = tomllib.loads((task_dir / "task.toml").read_text())
        assert task_config["task"]["name"] == "minds-evals/{}".format(task_dir.name)
        assert task_config["metadata"]["mngr_sha"] == expected_sha
        # The workspace template is pinned to an exact SHA, recorded alongside
        # the branch it was resolved from.
        assert task_config["metadata"]["dwt_sha"] == expected_dwt_sha
        assert task_config["metadata"]["dwt_branch"] == "main"
        assert task_config["metadata"]["dwt_repo"] == str(dwt_repo.repo_dir)
        # Case budget + verification budget + grace, so verification never competes with the
        # conversation for time and teardown keeps its grace.
        assert task_config["agent"]["timeout_sec"] == 1800.0 + DEFAULT_VERIFICATION_TIMEOUT_SECONDS + 300.0
        assert task_config["verifier"]["environment_mode"] == "separate"
        assert task_config["verifier"]["env"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY}"
        assert set(task_config["artifacts"]) == {
            "/logs/agent/trajectory.json",
            "/logs/agent/state.json",
            # A directory, not a glob: harbor's artifact source is an exact path.
            "/logs/agent/verification",
        }

        # The instruction's fenced json block round-trips through the driver's parser.
        case_config = parse_case_config((task_dir / "instruction.md").read_text())
        assert case_config.case_id == task_dir.name
        assert case_config.mngr_sha == expected_sha
        assert case_config.dwt_sha == expected_dwt_sha

        # tests/ carries the verifier image inputs plus the case data -- and nothing else: a dev
        # checkout accumulates bytecode caches next to the template scripts, which must not ship.
        assert not list((task_dir / "tests").rglob("__pycache__"))
        tests_case = json.loads((task_dir / "tests" / "case.json").read_text())
        assert tests_case == case_config.model_dump(mode="json")
        assert (task_dir / "tests" / "Dockerfile").is_file()
        for expected_file in ("test.sh", "finalize.py", "gates/checks.py", "quality/judge.toml"):
            assert (task_dir / "tests" / VERIFIER_CRITERIA_DIRNAME / expected_file).is_file()

        # environment/ carries the box image inputs plus the staged mngr clone
        # (no .git: Modal's build-context upload drops it, so nothing may rely on it).
        assert (task_dir / "environment" / "Dockerfile").is_file()
        assert (task_dir / "environment" / "entrypoint.sh").is_file()
        assert (task_dir / "environment" / "mngr" / "README.md").is_file()
        assert not (task_dir / "environment" / "mngr" / ".git").exists()
        assert (task_dir / "environment" / "mngr_sha").read_text().strip() == expected_sha

        # The oracle writes its canned conversation as the ATIF trajectory the verifier grades: one
        # user step per prompt, each answered by an agent step.
        solve_text = (task_dir / "solution" / "solve.sh").read_text()
        assert "/logs/agent/trajectory.json" in solve_text
        assert "conversation.jsonl" not in solve_text
        assert "full_transcript.jsonl" not in solve_text
        oracle_trajectory = json.loads(render_oracle_trajectory_json(case_config))
        assert [step["source"] for step in oracle_trajectory["steps"]] == ["user", "agent"] * len(case_config.prompts)
        assert oracle_trajectory["extra"]["minds_evals"]["source"] == "hand_built"
        assert json.dumps(oracle_trajectory, indent=2) in solve_text

    # environment/ must be byte-identical across tasks or the Modal image cache diverges.
    digests = {_dir_content_digest(task_dir / "environment") for task_dir in task_dirs}
    assert len(digests) == 1


_EXPECTATIONS = {
    "outcome": "A working to-do web app delivered as a workspace app tab.",
    "deliverable": {"kind": "minds-app"},
    "test_commands": ["uv run pytest -q"],
}


def test_generate_dataset_emits_the_outcome_dimension_only_for_expectation_cases(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config = _valid_config(
        dwt_repo=str(dwt_repo.repo_dir),
        personas=[
            {"id": "todo-app", "prompts": ["Build it", "Sounds good."], "expectations": _EXPECTATIONS},
            {"id": "greeting", "prompts": ["hi", "Sounds good."]},
        ],
    )
    output_dir = tmp_path / "dataset"

    generate_dataset(config_path=_write_config(tmp_path, config), output_dir=output_dir, mngr_repo=str(repo.repo_dir))

    # rewardkit turns every immediate tests/ subdirectory into a scoring dimension, so a case with
    # nothing to score must not get the directory at all -- otherwise it would emit a partial
    # outcome score.
    assert not (output_dir / "greeting" / "tests" / VERIFIER_CRITERIA_DIRNAME / "outcome").exists()
    outcome_dir = output_dir / "todo-app" / "tests" / VERIFIER_CRITERIA_DIRNAME / "outcome"
    assert {path.name for path in outcome_dir.iterdir()} == {"checks.py", "judge.toml", "prompt.md"}

    judge = tomllib.loads((outcome_dir / "judge.toml").read_text())
    # The last three are written by grade-time pre-steps and always exist: rewardkit renders a
    # listed path it cannot find as a "[not found]" block, so a conditional entry would put noise
    # into every flow-less trial's judge prompt.
    assert judge["judge"]["files"] == [
        "/logs/agent/expectations.md",
        "/logs/agent/verification/manifest.json",
        "/logs/agent/judge_transcript.txt",
        "/logs/agent/judge_flows_digest.txt",
        "/logs/agent/judge_screenshots",
    ]
    # rewardkit averages all .py criteria into ONE reward of weight 1.0 and weighs each judge toml
    # separately, so weight 1.0 is what makes the judge exactly half the dimension.
    assert judge["judge"]["weight"] == 1.0
    assert [criterion["name"] for criterion in judge["criterion"]] == ["works_as_expected"]


def test_generate_dataset_expands_expectations_identically_into_both_copies(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config = _valid_config(
        dwt_repo=str(dwt_repo.repo_dir),
        personas=[{"id": "todo-app", "prompts": ["Build it"], "expectations": _EXPECTATIONS}],
    )
    output_dir = tmp_path / "dataset"

    generate_dataset(config_path=_write_config(tmp_path, config), output_dir=output_dir, mngr_repo=str(repo.repo_dir))

    task_dir = output_dir / "todo-app"
    tests_case = json.loads((task_dir / "tests" / "case.json").read_text())
    instruction_case = parse_case_config((task_dir / "instruction.md").read_text())

    # The collector (instruction.md) and the judge (case.json) must read the identical expanded form.
    assert tests_case == instruction_case.model_dump(mode="json")
    expectations = tests_case["expectations"]
    assert [check["min_registered_apps"] for check in expectations["app_checks"]] == [1]
    assert [check["target"] for check in expectations["http_checks"]] == ["registered-apps"]
    assert expectations["test_commands"] == ["uv run pytest -q"]
    # The authored form rides along so a reader can see what the config actually said.
    assert tests_case["authored_expectations"]["deliverable"] == {
        "kind": "MINDS_APP",
        "min_registered_apps": None,
        "http": [],
        "files": [],
    }


def test_generate_dataset_fabricates_a_green_oracle_bundle_for_expectation_cases(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config = _valid_config(
        dwt_repo=str(dwt_repo.repo_dir),
        personas=[
            {"id": "todo-app", "prompts": ["Build it"], "expectations": _EXPECTATIONS},
            {"id": "greeting", "prompts": ["hi"]},
        ],
    )
    output_dir = tmp_path / "dataset"

    generate_dataset(config_path=_write_config(tmp_path, config), output_dir=output_dir, mngr_repo=str(repo.repo_dir))

    solve_text = (output_dir / "todo-app" / "solution" / "solve.sh").read_text()
    assert "/logs/agent/verification/manifest.json" in solve_text
    assert "/logs/agent/verification/apps.toml" in solve_text
    assert '"status": "failed"' not in solve_text
    # A case with nothing to verify keeps grading exactly as it did before: it gets the (empty)
    # evidence directory the artifact collector expects, and no fabricated evidence at all.
    greeting_solve = (output_dir / "greeting" / "solution" / "solve.sh").read_text()
    assert "mkdir -p /logs/agent/verification" in greeting_solve
    assert "manifest.json" not in greeting_solve


def test_load_eval_config_rejects_a_malformed_expectations_block(tmp_path: Path) -> None:
    config = _valid_config(
        personas=[{"id": "bad", "prompts": ["hi"], "expectations": {"outcome": "x", "deliverable": {"kind": "nope"}}}]
    )

    with pytest.raises(EvalConfigError, match="unknown deliverable kind"):
        load_eval_config(_write_config(tmp_path, config))


def test_generate_dataset_rejects_nonempty_output_dir(tmp_path: Path) -> None:
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config_path = _write_config(tmp_path, _valid_config(dwt_repo=str(dwt_repo.repo_dir)))
    output_dir = tmp_path / "dataset"
    output_dir.mkdir()
    (output_dir / "leftover.txt").write_text("stale")

    with pytest.raises(EvalConfigError, match="not empty"):
        generate_dataset(config_path=config_path, output_dir=output_dir, mngr_repo=str(repo.repo_dir))


def _stepped_config() -> dict[str, Any]:
    """A three-step roadmap case: build from an uploaded dataset, adjust it, then update the data."""
    config: dict[str, Any] = {
        "mngr_branch": "main",
        "timeout_seconds": 3600,
        "personas": [
            {
                "id": "project-roadmap",
                "persona": "Non-technical program lead.",
                "steps": [
                    {
                        "name": "build-from-data",
                        "files": [{"source": "uploads/v1", "upload_id": "pull-one"}],
                        "prompts": [
                            "Build me a roadmap view from the export in /home/user/workspace/data/uploads/pull-one.",
                            {"goal": "See a mockup and sign off on it", "max_exchanges": 4},
                        ],
                        "expectations": {"outcome": "A mockup was presented and the client approved it."},
                        "min_reward": {"gates": 1.0, "outcome": 0.5},
                    },
                    {
                        "name": "adjust-requirements",
                        "prompts": [
                            "A third of the milestones have a second owning team.",
                            {"goal": "Confirm the roadmap handles two owning teams", "max_exchanges": 3},
                        ],
                        "expectations": {
                            "outcome": "A running roadmap with a filter by team.",
                            "deliverable": {"kind": "minds-app"},
                        },
                        "min_reward": 0.4,
                    },
                    {
                        "name": "updated-dataset",
                        "files": [{"source": "uploads/v2", "upload_id": "pull-two"}],
                        "prompts": [
                            "The updated pull is in /home/user/workspace/data/uploads/pull-two.",
                            {"goal": "Confirm the roadmap reflects the updated data", "max_exchanges": 3},
                        ],
                        "expectations": {
                            "outcome": "The running roadmap reflects the updated export.",
                            "deliverable": {"kind": "minds-app"},
                            "ui_flows": [
                                {
                                    "name": "updated-content",
                                    "steps": "Open the roadmap.",
                                    "expect": "The updated export's milestones are shown.",
                                }
                            ],
                        },
                    },
                ],
            }
        ],
    }
    return config


def _write_upload_sources(tmp_path: Path) -> None:
    """The two dataset directories `_stepped_config`'s steps ship, beside the eval config."""
    for pull, content in (("v1", "id,title\nM-1,First\n"), ("v2", "id,title\nM-1,First\nM-2,Second\n")):
        pull_dir = tmp_path / "uploads" / pull
        pull_dir.mkdir(parents=True, exist_ok=True)
        (pull_dir / "milestones.csv").write_text(content)


def _generate_one_task(tmp_path: Path, config: dict[str, Any]) -> Path:
    mngr_repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=1)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    config["dwt_repo"] = str(dwt_repo.repo_dir)
    task_dirs = generate_dataset(
        config_path=_write_config(tmp_path, config),
        output_dir=tmp_path / "dataset",
        mngr_repo=str(mngr_repo.repo_dir),
    )
    return task_dirs[0]


def _generate_stepped_task(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    _write_upload_sources(tmp_path)
    return _generate_one_task(tmp_path, config if config is not None else _stepped_config())


def _load_stepped_config(tmp_path: Path, config: dict[str, Any]) -> EvalConfig:
    _write_upload_sources(tmp_path)
    return load_eval_config(_write_config(tmp_path, config))


def test_load_eval_config_flattens_a_stepped_cases_prompts_into_the_cases_own_list(tmp_path: Path) -> None:
    """Everything that reasons about a case as a whole -- the oracle, the timeout warning -- reads
    `prompts`, so a stepped case has to answer that question too."""
    case = _load_stepped_config(tmp_path, _stepped_config()).cases[0]

    assert case.steps is not None
    assert [step.name for step in case.steps] == ["build-from-data", "adjust-requirements", "updated-dataset"]
    assert case.prompts == tuple(entry for step in case.steps for entry in step.prompts)
    assert case.expectations is None
    assert case.reward_strategy is RewardStrategy.FINAL


def test_load_eval_config_parses_a_steps_files_expectations_and_reward_floor(tmp_path: Path) -> None:
    case = _load_stepped_config(tmp_path, _stepped_config()).cases[0]

    assert case.steps is not None
    first, second, final = case.steps
    assert first.files == (StepFile(source="uploads/v1", upload_id="pull-one"),)
    assert first.expectations is not None and first.expectations.deliverable is None
    assert isinstance(first.min_reward, PerDimensionRewardFloors)
    assert [(floor.dimension.value, floor.floor) for floor in first.min_reward.floors] == [
        ("gates", 1.0),
        ("outcome", 0.5),
    ]
    # A bare number is kept as a bare number, so the generated task.toml says what the config said.
    assert second.min_reward == ComposedRewardFloor(floor=0.4)
    assert second.files == ()
    # The last step may not gate, since harbor's threshold only ever aborts the steps after it.
    assert final.min_reward is None
    assert final.files == (StepFile(source="uploads/v2", upload_id="pull-two"),)


def test_load_eval_config_rejects_a_min_reward_on_the_last_step(tmp_path: Path) -> None:
    """harbor's threshold only aborts LATER steps, so one on the last step would be graded and then
    ignored -- an eval author must be told, not quietly given a no-op."""
    config = _stepped_config()
    config["personas"][0]["steps"][2]["min_reward"] = 1.0

    with pytest.raises(EvalConfigError, match="the last step cannot declare a 'min_reward'"):
        _load_stepped_config(tmp_path, config)


@pytest.mark.parametrize(
    ("min_reward", "message"),
    [
        ({"nonsense": 1.0}, "is not a reward dimension"),
        ({"gates": "high"}, "must be a number"),
        ({}, "gates nothing"),
        ("high", "must be a number or an object"),
    ],
)
def test_load_eval_config_rejects_a_min_reward_it_cannot_gate_on(
    tmp_path: Path, min_reward: object, message: str
) -> None:
    config = _stepped_config()
    config["personas"][0]["steps"][0]["min_reward"] = min_reward

    with pytest.raises(EvalConfigError, match=message):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_an_outcome_floor_on_a_step_that_is_not_graded_on_one(tmp_path: Path) -> None:
    """The outcome directory is written only for a step that declares expectations, so a step
    without them emits no outcome score -- and harbor reads a threshold on a key the verifier never
    wrote as -inf, aborting the trial there on every run whatever the agent did."""
    config = _stepped_config()
    del config["personas"][0]["steps"][0]["expectations"]

    with pytest.raises(EvalConfigError, match="declares no 'expectations'"):
        _load_stepped_config(tmp_path, config)


@pytest.mark.parametrize(
    ("min_reward", "expected_floor"),
    [
        (0.4, ComposedRewardFloor(floor=0.4)),
        ({"gates": 1.0}, PerDimensionRewardFloors(floors=(RewardFloor(dimension=RewardDimension.GATES, floor=1.0),))),
        (
            {"quality": 0.5},
            PerDimensionRewardFloors(floors=(RewardFloor(dimension=RewardDimension.QUALITY, floor=0.5),)),
        ),
        (
            {"reward": 0.3},
            PerDimensionRewardFloors(floors=(RewardFloor(dimension=RewardDimension.REWARD, floor=0.3),)),
        ),
    ],
)
def test_load_eval_config_allows_the_always_scored_floors_on_a_step_without_expectations(
    tmp_path: Path, min_reward: object, expected_floor: StepMinReward
) -> None:
    """gates and quality ship in every verifier build context and `reward` is what finalize.py
    composes, so all three are gradeable however the step is configured."""
    config = _stepped_config()
    del config["personas"][0]["steps"][0]["expectations"]
    config["personas"][0]["steps"][0]["min_reward"] = min_reward

    case = _load_stepped_config(tmp_path, config).cases[0]

    # The parsed floor, not merely a non-None one: a parser that read every dimension as `gates`
    # would satisfy all four of these cases.
    assert case.steps is not None and case.steps[0].min_reward == expected_floor


def test_load_eval_config_rejects_case_level_expectations_on_a_stepped_case(tmp_path: Path) -> None:
    """Every step states its own, so a reader of a step's instruction sees what that step is graded
    on rather than one block that applies to none of them in full."""
    config = _stepped_config()
    config["personas"][0]["expectations"] = {"outcome": "Something ran.", "deliverable": {"kind": "minds-app"}}

    with pytest.raises(EvalConfigError, match="states its expectations per step"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_a_case_that_says_its_turns_twice(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["prompts"] = ["Build it"]

    with pytest.raises(EvalConfigError, match="declares both 'prompts' and 'steps'"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_a_step_name_that_cannot_name_a_directory(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["steps"][0]["name"] = "Build From Data"

    with pytest.raises(EvalConfigError, match="must match"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_duplicate_step_names(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["steps"][1]["name"] = "build-from-data"

    with pytest.raises(EvalConfigError, match="duplicate step name"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_an_upload_id_used_twice_in_a_case(tmp_path: Path) -> None:
    """An id names one directory under the workspace's data/uploads/, so a repeat would have a later
    step overwrite an upload the client is still referring to by path."""
    config = _stepped_config()
    config["personas"][0]["steps"][2]["files"][0]["upload_id"] = "pull-one"

    with pytest.raises(EvalConfigError, match="duplicate upload_id"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_names_the_step_a_bad_prompt_belongs_to(tmp_path: Path) -> None:
    """Prompt errors are counted within one step, so a message naming only the case leaves the
    author looking through all of them."""
    config = _stepped_config()
    config["personas"][0]["steps"][1]["prompts"] = ["Fold it in", "  "]

    with pytest.raises(EvalConfigError, match="step adjust-requirements' prompt 2"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_a_files_entry_that_is_not_a_list(tmp_path: Path) -> None:
    """An object where the list belongs is falsy, so reading it as "no files" would generate a step
    whose prompts quote an upload path nothing ever staged."""
    config = _stepped_config()
    config["personas"][0]["steps"][0]["files"] = {"source": "uploads/v1", "upload_id": "pull-one"}

    with pytest.raises(EvalConfigError, match="'files' must be a list"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_an_upload_whose_source_is_not_there(tmp_path: Path) -> None:
    """Checked at load time rather than when the copy is attempted, so a mistyped path fails before
    any remote is resolved or any task directory is written."""
    config = _stepped_config()
    config["personas"][0]["steps"][0]["files"][0]["source"] = "uploads/missing"

    with pytest.raises(EvalConfigError, match="has no source at"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_an_upload_source_outside_the_config_directory(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["steps"][0]["files"][0]["source"] = "../elsewhere"

    with pytest.raises(EvalConfigError, match="must be a relative path"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_rejects_a_reward_strategy_on_a_flat_case(tmp_path: Path) -> None:
    with pytest.raises(EvalConfigError, match="but no 'steps'"):
        load_eval_config(
            _write_config(
                tmp_path, _valid_config(personas=[{"id": "flat", "prompts": ["Build it"], "reward_strategy": "mean"}])
            )
        )


def test_load_eval_config_rejects_an_unknown_reward_strategy(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["reward_strategy"] = "median"

    with pytest.raises(EvalConfigError, match="unknown reward_strategy"):
        _load_stepped_config(tmp_path, config)


def test_load_eval_config_lets_a_later_step_open_with_a_goal_entry(tmp_path: Path) -> None:
    """Only the case's opening ask must be literal; a later step opens mid-conversation, where there
    is a transcript for the client to decide from."""
    config = _stepped_config()
    config["personas"][0]["steps"][1]["prompts"] = [{"goal": "Push until the filter works"}]

    case = _load_stepped_config(tmp_path, config).cases[0]

    assert case.steps is not None
    assert case.steps[1].prompts == (
        GoalEntry(goal="Push until the filter works", max_exchanges=DEFAULT_MAX_EXCHANGES),
    )


def test_generate_dataset_writes_a_stepped_task_harbor_can_load(tmp_path: Path) -> None:
    """The cheapest strong proof the shape is right: harbor's own Task model validates the directory,
    finds every step's instruction, and reports the steps the generator declared."""
    task_dir = _generate_stepped_task(tmp_path)

    task = Task(task_dir)

    assert Task.is_valid_dir(task_dir)
    assert task.has_steps
    assert task.config.steps is not None
    assert [step.name for step in task.config.steps] == [
        "build-from-data",
        "adjust-requirements",
        "updated-dataset",
    ]
    assert task.config.multi_step_reward_strategy is MultiStepRewardStrategy.FINAL
    # A multi-step task has NO top-level instruction, tests or solution; harbor reads each step's own
    # and would leave the top-level ones unread.
    assert not (task_dir / "instruction.md").exists()
    assert not (task_dir / "tests").exists()
    assert not (task_dir / "solution").exists()
    assert task.instruction == ""
    assert "build-from-data" in task.step_instruction("build-from-data")


def test_generate_dataset_selects_the_authored_reward_strategy(tmp_path: Path) -> None:
    config = _stepped_config()
    config["personas"][0]["reward_strategy"] = "mean"

    task = Task(_generate_stepped_task(tmp_path, config))

    assert task.config.multi_step_reward_strategy is MultiStepRewardStrategy.MEAN


def test_generate_dataset_gives_each_step_its_own_case_config(tmp_path: Path) -> None:
    task_dir = _generate_stepped_task(tmp_path)

    first = parse_case_config((task_dir / "steps" / "build-from-data" / "instruction.md").read_text())
    second = parse_case_config((task_dir / "steps" / "adjust-requirements" / "instruction.md").read_text())
    final = parse_case_config((task_dir / "steps" / "updated-dataset" / "instruction.md").read_text())

    # Each step's config carries only its own turns, plus where it sits -- which is how the driver
    # knows which run() is the last one, the one that tears the workspace down.
    assert first.prompts == (
        "Build me a roadmap view from the export in /home/user/workspace/data/uploads/pull-one.",
        GoalEntry(goal="See a mockup and sign off on it", max_exchanges=4),
    )
    assert first.step is not None and (first.step.index, first.step.total) == (0, 3)
    assert not is_final_step(first.step)
    assert final.step is not None and is_final_step(final.step)
    # Each step is graded against its own expectations, expanded exactly as a flat case's are.
    assert first.expectations is not None and first.expectations.app_checks == ()
    assert not first.expectations.is_deliverable_bundle_required
    assert second.expectations is not None and second.expectations.app_checks != ()
    assert final.expectations is not None and len(final.expectations.ui_flow_checks) == 1
    # The case's identity and pins ride every step unchanged: a step is a stretch of one case, not a
    # case of its own.
    assert (first.case_id, first.persona, first.dwt_sha) == (final.case_id, final.persona, final.dwt_sha)


def test_generate_dataset_tells_each_step_how_many_entries_precede_it(tmp_path: Path) -> None:
    """The per-entry records accumulate across the conversation while a step's case file holds only
    its own turns, so the structural gates need the count the earlier steps configured."""
    task_dir = _generate_stepped_task(tmp_path)

    configs = [
        parse_case_config((task_dir / "steps" / name / "instruction.md").read_text())
        for name in ("build-from-data", "adjust-requirements", "updated-dataset")
    ]

    assert [config.step.entries_before for config in configs if config.step is not None] == [0, 2, 4]


def test_generate_dataset_ships_every_step_the_whole_verifier(tmp_path: Path) -> None:
    """In separate mode harbor REPLACES the verifier build context with a step's tests rather than
    overlaying it, so every step has to carry the complete verifier -- with its own case.json."""
    task_dir = _generate_stepped_task(tmp_path)

    for name in ("build-from-data", "adjust-requirements", "updated-dataset"):
        tests_dir = task_dir / "steps" / name / "tests"
        assert (tests_dir / "Dockerfile").is_file()
        assert (tests_dir / VERIFIER_CRITERIA_DIRNAME / "test.sh").is_file()
        assert (tests_dir / VERIFIER_CRITERIA_DIRNAME / "gates" / "checks.py").is_file()
        assert (tests_dir / VERIFIER_CRITERIA_DIRNAME / "quality" / "judge.toml").is_file()
        # Every step here declares expectations, so every one gets the outcome dimension.
        assert (tests_dir / VERIFIER_CRITERIA_DIRNAME / "outcome" / "judge.toml").is_file()
        case_json = json.loads((tests_dir / "case.json").read_text())
        assert case_json["step"]["name"] == name

    # The criteria are one COPY and the case file another, in that order, so the per-step images
    # share every layer but the case data.
    dockerfile = (task_dir / "steps" / "build-from-data" / "tests" / "Dockerfile").read_text()
    assert dockerfile.index("COPY verifier /tests") < dockerfile.index("COPY case.json /tests/case.json")


def test_a_step_that_commissions_nothing_still_has_an_outcome_dimension(tmp_path: Path) -> None:
    """Expectations with no deliverable register no programmatic criteria at all, so the whole
    composition rests on rewardkit still producing an `outcome` reward from the judge alone: without
    one, the verifier reads a declared outcome that produced no score and errors the trial.

    rewardkit's discovery is used directly because running the public entry point would make a live
    judge call. The version it comes from is pinned to the verifier container's by
    ``rewardkit_pin_test``.
    """
    task_dir = _generate_stepped_task(tmp_path)

    criteria_dir = task_dir / "steps" / "build-from-data" / "tests" / VERIFIER_CRITERIA_DIRNAME
    rewards = discover(criteria_dir, workspace=str(tmp_path))

    assert "outcome" in {reward.name for reward in rewards}


def test_generate_dataset_gives_a_step_without_expectations_no_outcome_dimension(tmp_path: Path) -> None:
    """The outcome directory is a scoring dimension, so a step with nothing to score must not have
    one -- rewardkit would otherwise emit a partial score for it."""
    config = _stepped_config()
    del config["personas"][0]["steps"][0]["expectations"]
    # A step with nothing to score cannot gate on the outcome dimension either.
    config["personas"][0]["steps"][0]["min_reward"] = {"gates": 1.0}

    task_dir = _generate_stepped_task(tmp_path, config)

    tests_dir = task_dir / "steps" / "build-from-data" / "tests"
    assert not (tests_dir / VERIFIER_CRITERIA_DIRNAME / "outcome").exists()
    assert json.loads((tests_dir / "case.json").read_text())["expectations"] is None


def test_generate_dataset_stages_a_steps_files_for_harbor_to_upload(tmp_path: Path) -> None:
    """Harbor merges a step's workdir into the box's working directory and then runs setup.sh from
    there, which is the only channel from the task directory into the box."""
    task_dir = _generate_stepped_task(tmp_path)

    workdir = task_dir / "steps" / "build-from-data" / "workdir"
    assert (workdir / STEP_FILES_DIRNAME / "pull-one" / "milestones.csv").read_text() == "id,title\nM-1,First\n"
    setup_script = workdir / "setup.sh"
    assert setup_script.stat().st_mode & 0o111
    setup_text = setup_script.read_text()
    assert step_files_box_dir("build-from-data") in setup_text
    # The script relocates the uploads and then removes itself, so the box's working directory --
    # the mngr checkout every workspace is vendored from -- is left as the image shipped it.
    assert "rm -f setup.sh" in setup_text
    assert "$destination" not in setup_text

    # A step that introduces nothing writes no workdir at all, which harbor treats as nothing to do.
    assert not (task_dir / "steps" / "adjust-requirements" / "workdir").exists()


def test_generate_dataset_points_a_steps_config_at_the_box_copy_of_its_files(tmp_path: Path) -> None:
    """The driver never sees the task directory, so the only way it can find a step's uploads is the
    box path the config names."""
    task_dir = _generate_stepped_task(tmp_path)

    first = parse_case_config((task_dir / "steps" / "build-from-data" / "instruction.md").read_text())

    assert first.step is not None
    assert [(entry.upload_id, entry.box_path) for entry in first.step.files] == [
        ("pull-one", "{}/pull-one".format(step_files_box_dir("build-from-data")))
    ]
    # The instruction names both ends of the copy, so a reader can follow the file from the task
    # directory to the path the prompts quote.
    instruction = (task_dir / "steps" / "build-from-data" / "instruction.md").read_text()
    assert "/home/user/workspace/data/uploads/pull-one" in instruction


def test_generate_dataset_splits_the_conversation_budget_across_the_steps(tmp_path: Path) -> None:
    """harbor applies the task agent timeout to EVERY step unless the step overrides it, so without
    a split a three-step case would be allowed to run for three times its declared budget."""
    task_dir = _generate_stepped_task(tmp_path)

    task_config = tomllib.loads((task_dir / "task.toml").read_text())
    configs = [
        parse_case_config((task_dir / "steps" / name / "instruction.md").read_text())
        for name in ("build-from-data", "adjust-requirements", "updated-dataset")
    ]

    assert sum(config.timeout_seconds for config in configs) == pytest.approx(3600.0)
    # Five worst-case exchanges in the first step against four in each of the others.
    assert configs[0].timeout_seconds > configs[1].timeout_seconds
    # Anything that outlives the step it was started in -- the reverse tunnel -- is sized against
    # the trial's whole lifetime, which is more than the conversation total: between two
    # conversations the trial also spends a step's evidence phase, cleanup grace and verifier.
    assert configs[0].step is not None
    assert configs[0].step.trial_lifetime_seconds == pytest.approx(
        3600.0 + 3 * (DEFAULT_VERIFICATION_TIMEOUT_SECONDS + AGENT_TIMEOUT_GRACE_SECONDS + VERIFIER_TIMEOUT_SECONDS)
    )
    # And it really does exceed the wall clock harbor will allow the steps themselves.
    assert configs[0].step.trial_lifetime_seconds > sum(
        step_toml["agent"]["timeout_sec"] for step_toml in task_config["steps"]
    )
    # Every step collects evidence, so every step is given the verification budget on top of its
    # conversation share.
    for step_toml, config in zip(task_config["steps"], configs, strict=True):
        assert step_toml["agent"]["timeout_sec"] == pytest.approx(
            config.timeout_seconds + DEFAULT_VERIFICATION_TIMEOUT_SECONDS + AGENT_TIMEOUT_GRACE_SECONDS
        )
        # Restated per step, so the figure a reader of a step sees is the one that step gets.
        assert step_toml["verifier"]["timeout_sec"] == VERIFIER_TIMEOUT_SECONDS


def test_a_stepped_case_can_outlast_the_one_workspace_its_steps_share() -> None:
    """One workspace serves every step, and its sandbox lifetime is a ceiling no case config can
    raise. A case whose steps together outlast it loses the workspace mid-trial and is recorded as
    a harness failure rather than as anything about the agent, so generation has to say so."""
    over_budget = EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS
    assert is_trial_longer_than_the_workspace(over_budget, DEFAULT_VERIFICATION_TIMEOUT_SECONDS, step_count=3)
    # The same conversation budget spent by one step is a flat case's, and fits comfortably.
    assert not is_trial_longer_than_the_workspace(1800.0, 300.0, step_count=3)


def test_generate_dataset_warns_before_it_builds_a_stepped_case_too_long_for_its_workspace(
    tmp_path: Path, logged_warnings: list[str]
) -> None:
    config = _stepped_config()
    config["timeout_seconds"] = EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS

    _generate_stepped_task(tmp_path, config)

    assert any("the one workspace they share is capped at" in message for message in logged_warnings)


def test_generate_dataset_never_holds_a_flat_case_to_the_cross_step_lifetime(
    tmp_path: Path, logged_warnings: list[str]
) -> None:
    """A flat case's one `run()` creates the workspace and tears it down, so there is no lifetime
    spanning steps to exceed however long its budget is."""
    _generate_one_task(tmp_path, _valid_config(timeout_seconds=10 * EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS))

    assert not any("workspace they share" in message for message in logged_warnings)


def test_generate_dataset_passes_each_steps_reward_floor_through_to_harbor(tmp_path: Path) -> None:
    task_dir = _generate_stepped_task(tmp_path)

    steps_toml = tomllib.loads((task_dir / "task.toml").read_text())["steps"]

    assert steps_toml[0]["min_reward"] == {"gates": 1.0, "outcome": 0.5}
    assert steps_toml[1]["min_reward"] == 0.4
    assert "min_reward" not in steps_toml[2]


def test_render_min_reward_toml_keeps_the_form_its_author_wrote() -> None:
    """A bare number gates the composed reward and a mapping gates each dimension it names, so the
    two must not be normalised into one."""
    assert render_min_reward_toml(ComposedRewardFloor(floor=0.4)) == "min_reward = 0.4"
    mapping = PerDimensionRewardFloors(
        floors=(
            RewardFloor(dimension=RewardDimension.GATES, floor=1.0),
            RewardFloor(dimension=RewardDimension.OUTCOME, floor=0.5),
        ),
    )

    assert tomllib.loads(render_min_reward_toml(mapping))["min_reward"] == {"gates": 1.0, "outcome": 0.5}


def test_a_reward_floor_mapping_cannot_be_empty() -> None:
    """An empty mapping renders as `min_reward = { }`, which harbor reads as a gate that can never
    fail: the step would declare a threshold it does not have and the trial would run to the end
    looking fine. The two forms are separate types for the same reason -- neither and both are the
    other two shapes that would render TOML the eval config did not ask for."""
    with pytest.raises(ValidationError):
        PerDimensionRewardFloors(floors=())


def _oracle_state(solve_text: str) -> dict[str, Any]:
    """The state.json one generated oracle script writes, read back out of its heredoc."""
    body = solve_text.split("<< 'MINDS_EVALS_STATE_EOF'\n", 1)[1]
    return json.loads(body.split("\nMINDS_EVALS_STATE_EOF", 1)[0])


def test_generate_dataset_gives_each_step_an_oracle_of_the_conversation_so_far(tmp_path: Path) -> None:
    """harbor prefers a step's own solution over the task's, and it must: the record each step has
    to fabricate is cumulative, so a task-level script replaying the whole case into every step
    would fail every earlier step's turn gate."""
    task_dir = _generate_stepped_task(tmp_path)

    entry_counts = []
    for name in ("build-from-data", "adjust-requirements", "updated-dataset"):
        state = _oracle_state((task_dir / "steps" / name / "solution" / "solve.sh").read_text())
        entry_counts.append((state["num_turns"], state["waits_done"], len(state["entries"])))

    assert entry_counts == [(2, 2, 2), (4, 4, 4), (6, 6, 6)]


def test_the_oracles_per_step_state_satisfies_that_steps_turn_gate(tmp_path: Path, gate_checks: ModuleType) -> None:
    """The whole per-step oracle rationale in one check: each step's fabricated state has to
    reconcile with the case file that step's verifier reads, or `harbor run -a oracle` scores 0 and
    aborts at the first gate."""
    task_dir = _generate_stepped_task(tmp_path)

    for name in ("build-from-data", "adjust-requirements", "updated-dataset"):
        step_dir = task_dir / "steps" / name
        state = _oracle_state((step_dir / "solution" / "solve.sh").read_text())
        case_json = json.loads((step_dir / "tests" / "case.json").read_text())

        assert gate_checks.is_every_entry_completed(state, case_json), name


def test_generate_dataset_keeps_a_flat_case_single_step(tmp_path: Path) -> None:
    """A case that declares no steps must generate exactly what it did before: a top-level
    instruction, tests and solution, no steps/ directory, and no multi-step reward strategy."""
    task_dir = _generate_one_task(tmp_path, _valid_config())

    task = Task(task_dir)

    assert not task.has_steps
    assert not (task_dir / "steps").exists()
    assert (task_dir / "instruction.md").is_file()
    assert (task_dir / "tests" / "case.json").is_file()
    assert (task_dir / "solution" / "solve.sh").is_file()
    assert parse_case_config((task_dir / "instruction.md").read_text()).step is None
    assert "steps" not in tomllib.loads((task_dir / "task.toml").read_text())


def test_the_shipped_stepped_eval_config_generates_a_loadable_task(tmp_path: Path) -> None:
    """The stepped config that ships with this repo, and the datasets beside it, must stay
    generatable as the schema moves."""
    config_dir = Path(__file__).parents[2] / "configs"
    config = json.loads((config_dir / "eval-config-stepped.json").read_text())
    shutil.copytree(config_dir / "datasets", tmp_path / "datasets")

    task_dir = _generate_one_task(tmp_path, config)

    task = Task(task_dir)
    assert task.has_steps
    assert task.config.steps is not None
    assert [step.name for step in task.config.steps] == [
        "build-from-data",
        "adjust-requirements",
        "updated-dataset",
    ]
    # The updated pull travels only with the step that introduces it, so it cannot be in the
    # workspace before that step places it.
    workdirs = sorted(path.parents[2].name for path in task_dir.glob("steps/*/workdir/step_files/*"))
    assert workdirs == ["build-from-data", "updated-dataset"]


def test_the_shipped_stepped_eval_config_fits_the_workspace_its_steps_share(tmp_path: Path) -> None:
    """The config an author copies must not be one the generator warns about. Its budgets are what
    keep the worst case under the eval overlay's sandbox lifetime, which no config can raise."""
    config_dir = Path(__file__).parents[2] / "configs"
    shutil.copytree(config_dir / "datasets", tmp_path / "datasets")
    shutil.copy(config_dir / "eval-config-stepped.json", tmp_path / "eval-config-stepped.json")

    config = load_eval_config(tmp_path / "eval-config-stepped.json")

    (case,) = config.cases
    assert case.steps is not None
    assert not is_trial_longer_than_the_workspace(
        config.timeout_seconds, config.verification_timeout_seconds, len(case.steps)
    )
    # And the conversation budget is still plausible for the messages the case can send, which is
    # the warning that pulling the budget down far enough would trip instead.
    assert not is_exchange_budget_implausible(case.prompts, config.timeout_seconds)
