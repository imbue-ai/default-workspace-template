import hashlib
import json
import tomllib
from pathlib import Path

import pytest
from inline_snapshot import snapshot
from pydantic import ValidationError

from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DEFAULT_AVG_WORD_COUNT_BASELINE
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.data_types import DEFAULT_MAX_EXCHANGES
from imbue.minds_evals.data_types import DEFAULT_VERIFICATION_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import MAX_EXCHANGES_CAP
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.errors import GitSourceError
from imbue.minds_evals.generate import TYPICAL_EXCHANGE_SECONDS
from imbue.minds_evals.generate import derive_case_id
from imbue.minds_evals.generate import generate_dataset
from imbue.minds_evals.generate import is_exchange_budget_implausible
from imbue.minds_evals.generate import load_eval_config
from imbue.minds_evals.generate import render_oracle_trajectory_json
from imbue.minds_evals.generate import render_prompt_entry_prose
from imbue.minds_evals.generate import resolve_remote_tip
from imbue.minds_evals.generate import worst_case_exchange_count
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

    assert prose == snapshot("(goal, up to 4 exchange(s)) Keep talking until satisfied: See it running")
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
        for expected_file in ("Dockerfile", "test.sh", "finalize.py", "gates/checks.py", "quality/judge.toml"):
            assert (task_dir / "tests" / expected_file).is_file()

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

    # rewardkit turns every tests/ subdirectory into a scoring dimension, so a case with nothing to
    # score must not get the directory at all -- otherwise it would emit a partial outcome score.
    assert not (output_dir / "greeting" / "tests" / "outcome").exists()
    outcome_dir = output_dir / "todo-app" / "tests" / "outcome"
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
