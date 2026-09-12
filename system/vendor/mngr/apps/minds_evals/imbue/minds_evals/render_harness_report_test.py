"""Unit tests for the grade-time harness-report renderer, which also settles which harness the trial
ran on and whether `harness_quality` applies to it. It ships as a self-contained verifier-container
script under templates/tests/verifier/ (stdlib only, not a package module), so the
`harness_report_renderer` fixture loads it by file path; the `finalize` fixture loads the reader of
the record it writes."""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from imbue.minds_evals.data_types import ArmRecord
from imbue.minds_evals.data_types import HarnessConfigRecord
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.testing import atif_document
from imbue.minds_evals.trajectory import build_hand_built_trajectory
from imbue.minds_evals.usage import summarize_workspace_usage

# The argument each tool carries its payload under, so a step reads the way the real document does.
# `bash` is pi-coding's name for the shell claude calls `Bash`.
PAYLOAD_KEYS = {"Bash": "command", "bash": "command", "Read": "file_path", "Agent": "prompt"}


def _step(
    index: int,
    message: str = "",
    command: str = "",
    skill: str = "",
    observation: str = "",
    is_error: bool = False,
    tool: str = "Bash",
) -> dict[str, Any]:
    """One agent step in the shape the workspace's own document records. ``command`` is the payload of
    whichever ``tool`` the step called: a shell command, a path read, a subagent's prompt."""
    call_id = "c{}".format(index)
    if skill:
        call: dict[str, Any] | None = {
            "tool_call_id": call_id,
            "function_name": "Skill",
            "arguments": {"skill": skill},
        }
    elif command:
        call = {"tool_call_id": call_id, "function_name": tool, "arguments": {PAYLOAD_KEYS[tool]: command}}
    else:
        # Neither is a message-only inference: the agent spoke and called nothing.
        call = None
    step: dict[str, Any] = {"step_id": index, "source": "agent", "message": message}
    if call is not None:
        step["tool_calls"] = [call]
    if observation:
        step["observation"] = {
            "results": [{"source_call_id": call_id, "content": observation, "extra": {"is_error": is_error}}]
        }
    return step


def test_a_run_whose_tooling_all_worked_keeps_only_its_skill_steps_and_counts_nothing(
    harness_report_renderer: ModuleType,
) -> None:
    steps = [
        _step(1, message="On it.", command="ls system/apps", observation="todo\nbrowser"),
        _step(2, skill="build-app", observation="Launching skill: build-app"),
        _step(3, command="uv run pytest -q", observation="47 passed"),
    ]

    report, counts = harness_report_renderer.render_harness_report(steps, "LEAD AGENT")

    assert counts == {}
    # The skill invocation is kept as evidence the harness held; the ordinary work is not.
    assert "build-app" in report
    assert "47 passed" not in report


def test_an_unresolvable_skill_is_counted_and_carries_its_error_into_the_report(
    harness_report_renderer: ModuleType,
) -> None:
    steps = [
        _step(
            1,
            skill="imbue-code-guardian:autofix",
            observation="<tool_use_error>Unknown skill: imbue-code-guardian:autofix</tool_use_error>",
            is_error=True,
        )
    ]

    report, counts = harness_report_renderer.render_harness_report(steps, "WORKER crystallize-todo")

    assert counts == {"unknown_skill": 1}
    assert "Unknown skill: imbue-code-guardian:autofix" in report
    assert "(ERROR)" in report


def test_each_failing_step_is_counted_under_its_most_specific_signature(
    harness_report_renderer: ModuleType,
) -> None:
    # The browser line also contains "No such file or directory"; it must count once, as the browser
    # failure it is, rather than twice.
    steps = [
        _step(
            1,
            command="ls /root/.cache/ms-playwright",
            observation="ls: cannot access '/root/.cache/ms-playwright': No such file or directory",
        ),
        _step(2, command="cat conftest.py", observation="cat: conftest.py: No such file or directory"),
        _step(3, command="uv run pytest", observation="ModuleNotFoundError: No module named 'playwright'"),
    ]

    _report, counts = harness_report_renderer.render_harness_report(steps, "WORKER w")

    assert counts == {"missing_browser": 1, "missing_path": 1, "missing_module": 1}


def test_the_agents_own_words_are_kept_so_a_recovery_can_be_told_from_a_surrender(
    harness_report_renderer: ModuleType,
) -> None:
    steps = [
        _step(
            1,
            message="The review plugin will not load; running the checks by hand instead.",
            skill="imbue-code-guardian:autofix",
            observation="<tool_use_error>Unknown skill: imbue-code-guardian:autofix</tool_use_error>",
            is_error=True,
        )
    ]

    report, _counts = harness_report_renderer.render_harness_report(steps, "WORKER w")

    assert "running the checks by hand instead" in report


def test_render_of_no_steps_says_so_rather_than_being_empty(harness_report_renderer: ModuleType) -> None:
    report, counts = harness_report_renderer.render_harness_report([], "LEAD AGENT")

    assert counts == {}
    assert "no skill invocation" in report


def test_load_steps_of_a_missing_or_malformed_file_is_empty(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    assert harness_report_renderer.load_steps(tmp_path / "does-not-exist.json") == []
    (tmp_path / "not-json.json").write_text("{not json")
    assert harness_report_renderer.load_steps(tmp_path / "not-json.json") == []


def test_worker_trajectories_are_found_by_name_and_a_dir_without_one_is_skipped(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "crystallize-todo").mkdir()
    (tmp_path / "crystallize-todo" / "trajectory.json").write_text(json.dumps({"steps": []}))
    (tmp_path / "reports-only").mkdir()

    found = harness_report_renderer.worker_trajectories(tmp_path)

    assert [name for name, _ in found] == ["crystallize-todo"]


def test_worker_trajectories_of_a_trial_that_launched_none_is_empty(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    assert harness_report_renderer.worker_trajectories(tmp_path / "absent") == []


def test_a_failure_string_inside_a_file_the_agent_read_is_not_a_failure(
    harness_report_renderer: ModuleType,
) -> None:
    # Reference docs and code comments quote the exact strings a broken harness prints. Counting them
    # scores an agent for the documentation it consulted, and buries a real regression in the noise.
    steps = [
        _step(
            1,
            command="/home/user/.agents/shared/worker/references/web-frontend-testing.md",
            tool="Read",
            observation="Do not write your own launch fixture, do not `playwright install` a managed browser.",
        )
    ]

    report, counts = harness_report_renderer.render_harness_report(steps, "WORKER w")

    assert counts == {}
    assert "no skill invocation" in report


def test_a_failure_string_inside_a_subagents_report_is_not_a_failure(
    harness_report_renderer: ModuleType,
) -> None:
    steps = [
        _step(
            1,
            command="Review the branch.",
            tool="Agent",
            observation="The suite follows web-frontend-testing.md: no ad-hoc `playwright install`, no skipping.",
        )
    ]

    _report, counts = harness_report_renderer.render_harness_report(steps, "WORKER w")

    assert counts == {}


def test_a_read_that_itself_errored_is_still_classified(harness_report_renderer: ModuleType) -> None:
    # The exclusion is about a tool reporting content, not about which tool ran: when the read itself
    # fails, the message is the harness speaking.
    steps = [
        _step(
            1,
            command="/home/user/workspace/conftest.py",
            tool="Read",
            observation="ModuleNotFoundError: No module named 'playwright'",
            is_error=True,
        )
    ]

    _report, counts = harness_report_renderer.render_harness_report(steps, "WORKER w")

    assert counts == {"missing_module": 1}


def test_the_browser_signature_names_the_failure_and_not_the_remedy(
    harness_report_renderer: ModuleType,
) -> None:
    # `playwright install` is what you run to fix a missing browser, so it appears in prose far more
    # often than in a failure. What identifies the failure is Playwright's own error.
    told_to_install = [
        _step(1, command="cat notes.md", observation="Run `playwright install` if a browser is missing.")
    ]
    actually_broken = [
        _step(
            1,
            command="uv run pytest",
            observation="Executable doesn't exist at /root/.cache/ms-playwright/chromium-1187/chrome-linux/chrome",
        )
    ]

    assert harness_report_renderer.render_harness_report(told_to_install, "WORKER w")[1] == {}
    assert harness_report_renderer.render_harness_report(actually_broken, "WORKER w")[1] == {"missing_browser": 1}


def test_a_missing_command_counts_whether_or_not_its_complaint_was_redirected(
    harness_report_renderer: ModuleType,
) -> None:
    # The same absent binary, run two ways. `2>/dev/null` swallows the shell's complaint and leaves
    # only the exit status, so keying on the message alone would score the agent's stderr handling
    # rather than the workspace that is missing `ss`.
    complained = [_step(1, command="ss -tln", observation="Exit code 127\n/bin/bash: line 1: ss: command not found")]
    redirected = [_step(1, command="ss -tln 2>/dev/null", observation="Exit code 127")]

    assert harness_report_renderer.render_harness_report(complained, "MAIN")[1] == {"missing_command": 1}
    assert harness_report_renderer.render_harness_report(redirected, "MAIN")[1] == {"missing_command": 1}


def _trial_tree(
    tmp_path: Path, main_steps: list[dict[str, Any]], workers: dict[str, list[dict[str, Any]]]
) -> dict[str, Path]:
    """A trial's captured logs, laid out the way the verifier container sees them."""
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps({"steps": main_steps}))
    workers_dir = tmp_path / "verification" / "workers"
    workers_dir.mkdir(parents=True)
    for name, steps in workers.items():
        (workers_dir / name).mkdir()
        (workers_dir / name / "trajectory.json").write_text(json.dumps({"steps": steps}))
    return {
        "trajectory_path": trajectory_path,
        "workers_dir": workers_dir,
        "main_report_path": tmp_path / "harness_main.txt",
        "worker_report_path": tmp_path / "harness_workers.txt",
        "counts_path": tmp_path / "harness_failures.json",
    }


def test_a_path_that_merely_names_a_browser_is_not_a_browser_failure(
    harness_report_renderer: ModuleType,
) -> None:
    # `missing_browser` sorts ahead of the uncounted `missing_path`, so anything it claims is scored.
    # Matching any absent path whose name contains "chrome" meant an agent's own working directories
    # counted as breakage, which is exactly what `missing_path` is uncounted to avoid. The browser
    # cache itself stays a browser failure: absent, there is no browser to drive.
    named_a_browser = [
        _step(
            1, command="ls chrome_profile", observation="ls: cannot access 'chrome_profile': No such file or directory"
        ),
        _step(2, command="rm chrome-data", observation="rm: cannot remove 'chrome-data': No such file or directory"),
    ]
    the_cache_itself = [
        _step(
            1,
            command="ls /root/.cache/ms-playwright",
            observation="ls: cannot access '/root/.cache/ms-playwright': No such file or directory",
        )
    ]

    assert harness_report_renderer.render_harness_report(named_a_browser, "WORKER w")[1] == {"missing_path": 1}
    assert harness_report_renderer.render_harness_report(the_cache_itself, "WORKER w")[1] == {"missing_browser": 1}


def test_a_missing_node_dependency_counts_like_a_missing_python_one(
    harness_report_renderer: ModuleType,
) -> None:
    # These evals commission browser apps, so the dominant missing-dependency failure is Node's.
    steps = [
        _step(
            1, command="node server.js", observation="Error: Cannot find module '/home/user/workspace/todo/server.js'"
        ),
        _step(2, command="npm test", observation="node:internal/modules/cjs/loader:1143 ERR_MODULE_NOT_FOUND"),
    ]

    assert harness_report_renderer.render_harness_report(steps, "WORKER w")[1] == {"missing_module": 2}


def test_only_a_shells_own_not_found_is_a_missing_command(
    harness_report_renderer: ModuleType,
) -> None:
    # `dash` prints "not found" where bash prints "command not found", so the bare form has to count --
    # but a tool's key/value output ends lines the same way and is not a missing binary.
    a_shell = [_step(1, command="ss -tln", observation="sh: 1: ss: not found")]
    a_tools_own_output = [_step(1, command="mngr status", observation="  status: not found\n  version: not found")]

    assert harness_report_renderer.render_harness_report(a_shell, "MAIN")[1] == {"missing_command": 1}
    assert harness_report_renderer.render_harness_report(a_tools_own_output, "MAIN")[1] == {}


def test_a_probe_for_a_path_is_reported_to_the_judge_but_not_counted_against_the_score(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    # An agent checking whether a path exists and finding it absent is ordinary exploration. Counting
    # it would score an agent that explores more as though its workspace were more broken.
    paths = _trial_tree(
        tmp_path,
        [
            _step(
                1, command="ls data/uploads", observation="ls: cannot access 'data/uploads': No such file or directory"
            )
        ],
        {},
    )

    recorded = harness_report_renderer.write_reports(**paths)

    assert recorded["main"]["counts"] == {"missing_path": 1}
    assert recorded["main"]["total"] == 1
    assert recorded["main"]["scored_total"] == 0
    assert "No such file or directory" in paths["main_report_path"].read_text()


def test_every_workers_failures_are_summed_and_each_worker_reaches_the_judge(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    paths = _trial_tree(
        tmp_path,
        [],
        {
            "crystallize-todo": [
                _step(
                    1,
                    skill="autofix",
                    observation="<tool_use_error>Unknown skill: autofix</tool_use_error>",
                    is_error=True,
                )
            ],
            "harden-ui": [
                _step(1, command="uv run pytest", observation="ModuleNotFoundError: No module named 'playwright'")
            ],
        },
    )

    recorded = harness_report_renderer.write_reports(**paths)

    assert recorded["workers"]["scored_total"] == 2
    assert recorded["workers"]["worker_count"] == 2
    # Both workers have to survive into the judge's file: if a later one is clipped away, the judge and
    # the scripted count disagree about the same trial, in the direction that hides breakage.
    report = paths["worker_report_path"].read_text()
    assert "crystallize-todo" in report
    assert "harden-ui" in report


def test_the_joined_worker_report_fits_the_judges_file_limit(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    # Each worker's report is budgeted, but MIN_WORKER_REPORT_CLIP is a floor, so enough workers
    # overrun the cap however the budget divides. rewardkit does not error on an oversized judge file;
    # it hands the judge `[skipped: file too large]`, which would leave the worker judge grading
    # nothing while the scripted criterion still counted every worker's signatures.
    noisy = [
        _step(
            index, command="uv run pytest", observation="ModuleNotFoundError: No module named 'x'" + "verbose " * 4000
        )
        for index in range(1, 12)
    ]
    paths = _trial_tree(tmp_path, [], {"worker-{:03d}".format(index): noisy for index in range(40)})

    recorded = harness_report_renderer.write_reports(**paths)

    assert recorded["workers"]["worker_count"] == 40
    assert len(paths["worker_report_path"].read_text()) <= harness_report_renderer.REPORT_CLIP


def test_a_trial_that_launched_no_worker_says_so_rather_than_leaving_an_empty_file(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    paths = _trial_tree(tmp_path, [_step(1, message="All done.")], {})

    recorded = harness_report_renderer.write_reports(**paths)

    assert recorded["workers"]["worker_count"] == 0
    assert recorded["workers"]["scored_total"] == 0
    assert "launched no worker" in paths["worker_report_path"].read_text()


def test_the_counts_file_carries_the_keys_the_scoring_criteria_read(
    harness_report_renderer: ModuleType, harness_checks: ModuleType, tmp_path: Path
) -> None:
    # The writer and the reader of this contract are otherwise only tested apart, so a renamed key
    # would pass both suites and silently score every trial clean.
    paths = _trial_tree(
        tmp_path,
        [_step(1, skill="autofix", observation="Unknown skill: autofix", is_error=True)],
        {},
    )
    harness_report_renderer.write_reports(**paths)

    assert harness_checks.scope_score("main", paths["counts_path"]) == harness_checks.score_for(1, 4)
    assert harness_checks.scope_score("workers", paths["counts_path"]) == 1.0


def test_an_unreadable_or_malformed_counts_file_scores_clean_rather_than_aborting_the_grade(
    harness_checks: ModuleType, tmp_path: Path
) -> None:
    # A criterion that raises aborts the whole grade in rewardkit, and a report this script failed to
    # write is a grading fault rather than a trial fault.
    assert harness_checks.scope_score("main", tmp_path / "absent.json") == 1.0
    (tmp_path / "not-json.json").write_text("{not json")
    assert harness_checks.scope_score("main", tmp_path / "not-json.json") == 1.0
    (tmp_path / "wrong-shape.json").write_text(json.dumps({"main": {"scored_total": "lots"}}))
    assert harness_checks.scope_score("main", tmp_path / "wrong-shape.json") == 1.0


def test_pis_lowercase_shell_output_is_read_as_testimony_about_the_harness(
    harness_report_renderer: ModuleType,
) -> None:
    # pi-coding names the shell `bash`, and what it prints is the same evidence claude's `Bash`
    # output is. A tool set that knows only claude's spelling classifies none of it -- the result is
    # not a lower score but no score at all, since nothing but an errored result is left to scan.
    steps = [
        _step(
            1,
            command="uv run pytest",
            observation="ModuleNotFoundError: No module named 'playwright'",
            tool="bash",
        )
    ]

    report, counts = harness_report_renderer.render_harness_report(steps, "LEAD AGENT")

    assert counts == {"missing_module": 1}
    assert "No module named 'playwright'" in report


def test_the_harness_is_the_one_the_captured_document_names(harness_report_renderer: ModuleType) -> None:
    workspace_document = atif_document()

    assert harness_report_renderer.harness_of_document(workspace_document) == "claude"
    assert (
        harness_report_renderer.harness_of_document({**workspace_document, "agent": {"name": "pi-coding"}})
        == "pi-coding"
    )
    assert harness_report_renderer.harness_of_document({**workspace_document, "agent": {"name": "codex"}}) == "codex"


@pytest.mark.parametrize(
    "document",
    (
        pytest.param({}, id="nothing_at_all"),
        pytest.param({"steps": []}, id="no_agent_block"),
        pytest.param({"agent": {}}, id="no_name"),
        pytest.param({"agent": {"name": ""}}, id="empty_name"),
        pytest.param({"agent": "claude"}, id="agent_not_an_object"),
        # What mngr writes when it cannot resolve the agent's type, which is a document that says
        # nothing rather than a harness of its own.
        pytest.param({"agent": {"name": "unknown"}}, id="unresolved_agent_type"),
    ),
)
def test_a_document_that_names_no_harness_is_read_as_claude(
    harness_report_renderer: ModuleType, document: dict[str, Any]
) -> None:
    # A document this pass cannot place has to keep every dimension rather than losing one.
    assert harness_report_renderer.harness_of_document(document) == "claude"


def _pi_arm_block() -> dict[str, Any]:
    """An arm block naming pi, as it reaches the verifier on a captured document's provenance."""
    return {"harness_config": {"harness": "pi-coding"}}


def _hand_built_document(arm: ArmRecord) -> dict[str, Any]:
    """The fallback trajectory the driver builds when the workspace could not hand over its own."""
    built = build_hand_built_trajectory(
        [{"role": "user", "text": "Build it"}, {"role": "agent", "text": "Done."}],
        TrajectoryProvenance(
            driver_name="minds-persona-driver",
            driver_version="0.1.0",
            decider_model="claude-opus-4-8",
            decider_turns=(),
            harbor_session_id="session-1",
            case_id="todo-app",
            usage_source=UsageSource.TRANSCRIPT,
            arm=arm,
        ),
        summarize_workspace_usage(()),
        timestamp="2026-09-01T00:00:00Z",
        boundaries=(),
    )
    assert built is not None
    return built.to_json_dict()


def test_the_drivers_hand_built_fallback_is_not_read_as_a_harness_of_its_own(
    harness_report_renderer: ModuleType,
) -> None:
    # The fallback document names the DRIVER in `agent.name`, because there was no captured document
    # to take a harness from. Reading that as the harness would strip harness_quality from every
    # claude trial whose transcript capture failed.
    document = _hand_built_document(ArmRecord())

    assert document["agent"]["name"] == "minds-persona-driver"
    assert harness_report_renderer.harness_of_document(document) == "claude"


def test_a_fallback_trajectory_takes_the_harness_from_the_config_the_driver_recorded(
    harness_report_renderer: ModuleType,
) -> None:
    # The harness config's harness is read back from the workspace's accounts listing rather than
    # from any document, so it is there even on a trial whose transcript capture failed -- which is
    # the only way such a trial can be kept off claude's judges.
    document = _hand_built_document(ArmRecord(harness_config=HarnessConfigRecord(lane="api-key", harness="pi-coding")))

    assert harness_report_renderer.harness_of_document(document) == "pi-coding"


def test_a_captured_document_names_its_own_harness_whatever_the_arm_recorded(
    harness_report_renderer: ModuleType,
) -> None:
    # The criteria are shaped after the agent that WROTE the trajectory, so on the shape that carries
    # that fact, it is the fact that decides.
    document = {
        **atif_document(),
        "extra": {"minds_evals": {"source": "workspace", "arm": _pi_arm_block()}},
    }

    assert harness_report_renderer.harness_of_document(document) == "claude"


def test_a_captured_document_whose_agent_type_is_unresolved_falls_back_to_the_arm(
    harness_report_renderer: ModuleType,
) -> None:
    # mngr writes `unknown` into a captured document too, and the arm is then the only thing left
    # that says what ran. Reading the placeholder as an answer would put a pi trajectory in front of
    # claude's judges and charge it the harness share of a dimension that measured nothing.
    document = {
        **atif_document(),
        "agent": {"name": "unknown"},
        "extra": {"minds_evals": {"source": "workspace", "arm": _pi_arm_block()}},
    }

    assert harness_report_renderer.harness_of_document(document) == "pi-coding"


def test_an_absent_or_unreadable_trajectory_is_read_as_claude(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    # A trajectory this script cannot read is a grading fault, and taking a dimension away over one
    # would quietly change what the trial was scored on.
    (tmp_path / "not-json.json").write_text("{not json")
    (tmp_path / "not-a-document.json").write_text("[1, 2]")

    for name in ("absent.json", "not-json.json", "not-a-document.json"):
        document = harness_report_renderer.load_trajectory_document(tmp_path / name)
        assert harness_report_renderer.harness_of_document(document) == "claude"


def _dimension_dir_with_a_judge(tmp_path: Path) -> Path:
    """The harness_quality dimension as it sits in the criteria tree the verifier image ships."""
    dimension_dir = tmp_path / "harness_quality"
    dimension_dir.mkdir()
    (dimension_dir / "judge_main.toml").write_text("[judge]\n")
    (dimension_dir / "checks.py").write_text("# criteria\n")
    return dimension_dir


def _prepare(harness_report_renderer: ModuleType, tmp_path: Path, document: dict[str, Any] | None) -> dict[str, Any]:
    """Settle the harness_quality applicability of a trial whose trajectory is the given document,
    over a criteria tree that has the dimension in it."""
    trajectory_path = tmp_path / "trajectory.json"
    if document is not None:
        trajectory_path.write_text(json.dumps(document))
    return harness_report_renderer.prepare_harness_quality(
        trajectory_path=trajectory_path,
        harness_path=tmp_path / "harness.json",
        dimension_dir=_dimension_dir_with_a_judge(tmp_path),
    )


def test_a_claude_trial_keeps_the_harness_quality_dimension(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    record = _prepare(harness_report_renderer, tmp_path, atif_document())

    assert record == {"name": "claude", "is_harness_quality_scored": True}
    assert (tmp_path / "harness_quality" / "judge_main.toml").is_file()
    assert json.loads((tmp_path / "harness.json").read_text()) == record


def test_a_pi_coding_trial_takes_the_harness_quality_dimension_out_of_the_criteria_tree(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    # rewardkit scores whatever dimension directories it finds, so removing the directory is the only
    # way to stop two opus judges from being paid to score claude signatures against a pi trajectory
    # that cannot contain any -- and to keep the 1.0 they would return out of the reward.
    record = _prepare(harness_report_renderer, tmp_path, {**atif_document(), "agent": {"name": "pi-coding"}})

    assert record == {"name": "pi-coding", "is_harness_quality_scored": False}
    assert not (tmp_path / "harness_quality").exists()
    assert json.loads((tmp_path / "harness.json").read_text()) == record


def test_a_trial_whose_trajectory_is_missing_keeps_the_dimension_it_always_had(
    harness_report_renderer: ModuleType, tmp_path: Path
) -> None:
    # A trajectory this pass cannot read is a grading fault rather than another harness, and the
    # dimension has to survive it: dropping it would quietly restate the reward of a claude trial.
    record = _prepare(harness_report_renderer, tmp_path, None)

    assert record == {"name": "claude", "is_harness_quality_scored": True}
    assert (tmp_path / "harness_quality" / "judge_main.toml").is_file()


def test_the_harness_record_is_written_in_the_shape_the_reward_composition_reads(
    harness_report_renderer: ModuleType, finalize: ModuleType, tmp_path: Path
) -> None:
    # The writer and the reader of this contract are in two scripts that cannot import each other,
    # so a renamed key would pass both suites and silently grade every pi trial on claude's terms.
    _prepare(harness_report_renderer, tmp_path, {**atif_document(), "agent": {"name": "pi-coding"}})

    assert finalize._harness_record(tmp_path / "harness.json") == ("pi-coding", False)
    assert finalize.DEFAULT_HARNESS == harness_report_renderer.CLAUDE_HARNESS
    # The path is as much of the contract as the keys, and the test above drives both scripts against
    # a tmp_path that hides a disagreement: a record written where the reader does not look reads as
    # a claude trial, so every pi trial would be charged for the dimension just taken away from it.
    assert harness_report_renderer.HARNESS_PATH == finalize.HARNESS_PATH
