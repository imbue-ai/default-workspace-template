"""Tests for ``plan_orchestration.py``.

Run via: ``uv run pytest .agents/skills/build-app-parallel/scripts/plan_orchestration_test.py``
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "plan_orchestration.py"
_spec = importlib.util.spec_from_file_location("plan_orchestration", _SCRIPT)
assert _spec is not None and _spec.loader is not None
plan_orchestration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_orchestration)

# A to-do list plan: two nodes start together, two interactive nodes, and a
# trailing worker node after the last conversation.
_TODO_PLAN = """<thinking>
Cut the spec out first so the scaffold and data layer can start early.
</thinking>
<output>
capability = ["high", "medium", "medium", "interactive", "medium", "high", "interactive", "medium"]
subtasks = ["Settle the spec.", "Scaffold the app.", "Build the mock.", "Show the mock.", "Build the storage.", "Build the real page.", "Show the working site.", "Hand off to crystallize-creation."]
access list = [[], [], [0, 1], [2], [0], [3, 4], [5], [6]]
</output>
"""


def _plan_text(capability: str, subtasks: str, access: str) -> str:
    return (
        f"<output>\ncapability = {capability}\nsubtasks = {subtasks}\n"
        f"access list = {access}\n</output>\n"
    )


def _write_run_dir(tmp_path: Path, plan_text: str) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.md").write_text(plan_text)
    return run_dir


def test_parse_plan_builds_nodes_with_access_and_models() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    nodes = plan["nodes"]
    assert [node["access"] for node in nodes] == [
        [],
        [],
        [0, 1],
        [2],
        [0],
        [3, 4],
        [5],
        [6],
    ]
    assert nodes[3]["capability"] == "interactive"
    assert nodes[3]["model"] is None
    assert [node["model"] for node in nodes if node["capability"] != "interactive"] == [
        plan_orchestration.MODEL_BY_CAPABILITY[node["capability"]]
        for node in nodes
        if node["capability"] != "interactive"
    ]
    assert nodes[7]["subtask"] == "Hand off to crystallize-creation."


def test_every_worker_capability_maps_to_opus_by_default() -> None:
    assert plan_orchestration.MODEL_BY_CAPABILITY == {
        "low": "opus",
        "medium": "opus",
        "high": "opus",
    }


def test_planner_prompt_examples_are_valid_plans() -> None:
    """The example plans the planner is shown must parse, so the prompt and the
    parser cannot drift apart."""
    prompt = (Path(__file__).parents[1] / "references" / "planner-prompt.md").read_text()
    example_blocks = re.findall(r"<output>\n(capability = .*?)\n</output>", prompt, re.DOTALL)

    assert len(example_blocks) == 2
    for block in example_blocks:
        plan = plan_orchestration.parse_plan(f"<output>\n{block}\n</output>")
        capabilities = [node["capability"] for node in plan["nodes"]]
        # A plan ends at the working-site conversation; the orchestrator does the
        # merge and the hardening handoff itself.
        assert capabilities[-1] == "interactive"


def test_parse_plan_expands_all_to_every_earlier_node() -> None:
    plan = plan_orchestration.parse_plan(
        _plan_text('["high", "low", "medium"]', '["a", "b", "c"]', '[[], [0], ["all"]]')
    )

    assert plan["nodes"][2]["access"] == [0, 1]


def test_parse_plan_uses_the_last_output_block() -> None:
    """Reasoning that quotes the tag before the real plan does not confuse parsing."""
    text = "<thinking>I will emit <output>x</output> below.</thinking>\n" + _plan_text(
        '["high", "low", "medium"]', '["a", "b", "c"]', "[[], [], [0, 1]]"
    )

    plan = plan_orchestration.parse_plan(text)

    assert len(plan["nodes"]) == 3


@pytest.mark.parametrize(
    ("plan_text", "message_fragment"),
    [
        ("no tags here", "no <output>"),
        (
            _plan_text('["high", "low"]', '["a", "b", "c"]', "[[], [], []]"),
            "differ in length",
        ),
        (
            _plan_text('["high", "low", "urgent"]', '["a", "b", "c"]', "[[], [], []]"),
            "capability 'urgent'",
        ),
        (
            _plan_text('["high", "low", "medium"]', '["a", "", "c"]', "[[], [], []]"),
            "empty or non-string subtask",
        ),
        (
            _plan_text('["high", "low", "medium"]', '["a", "b", "c"]', "[[], [1], []]"),
            "access entry 1",
        ),
        (
            _plan_text('["high", "low", "medium"]', '["a", "b", "c"]', "[[], [], [true]]"),
            "access entry True",
        ),
        (
            _plan_text('["high", "low", "medium"]', '["a", "b", "c"]', "[[], [], [0, 0]]"),
            "same node twice",
        ),
        (
            _plan_text('["high", "low"]', '["a", "b"]', "[[], []]"),
            "2 nodes",
        ),
        (
            "<output>\ncapability = [\"high\"\n</output>",
            "not a valid JSON list",
        ),
        (
            "<output>\ncapability = [\"high\", \"low\", \"medium\"]\nsubtasks = [\"a\", \"b\", \"c\"]\n</output>",
            "no 'access list",
        ),
    ],
)
def test_parse_plan_rejects_malformed_plans(plan_text: str, message_fragment: str) -> None:
    with pytest.raises(plan_orchestration.PlanError, match=message_fragment):
        plan_orchestration.parse_plan(plan_text)


def test_find_ready_nodes_starts_with_nodes_that_need_nothing() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    assert plan_orchestration.find_ready_nodes(plan, [], []) == [0, 1]


def test_find_ready_nodes_waits_for_every_listed_dependency() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    # Node 2 needs 0 and 1; with only 0 done, node 4 (needs 0) is the one that can start.
    assert plan_orchestration.find_ready_nodes(plan, [0], [1]) == [4]
    assert plan_orchestration.find_ready_nodes(plan, [0, 1], [4]) == [2]


def test_find_ready_nodes_respects_the_parallelism_cap() -> None:
    capability = json.dumps(["low"] * 8)
    subtasks = json.dumps([f"task {idx}" for idx in range(8)])
    plan = plan_orchestration.parse_plan(
        _plan_text(capability, subtasks, json.dumps([[]] * 8))
    )

    assert plan_orchestration.find_ready_nodes(plan, [], []) == [0, 1, 2, 3, 4]
    assert plan_orchestration.find_ready_nodes(plan, [], [0, 1, 2]) == [3, 4]
    assert plan_orchestration.find_ready_nodes(plan, [], [0, 1, 2, 3, 4]) == []


def test_interactive_nodes_take_no_worker_slot() -> None:
    """A conversation with the user neither uses a worker slot nor waits for one."""
    capability = json.dumps(["low"] * 5 + ["interactive", "low", "interactive"])
    subtasks = json.dumps([f"task {idx}" for idx in range(8)])
    access = json.dumps([[], [], [], [], [], [], [], [6]])
    plan = plan_orchestration.parse_plan(_plan_text(capability, subtasks, access))

    # Five workers fill every slot, and the conversation still starts.
    assert plan_orchestration.find_ready_nodes(plan, [], [0, 1, 2, 3, 4]) == [5]
    # A running conversation leaves both free slots to workers.
    assert plan_orchestration.find_ready_nodes(plan, [], [0, 1, 2, 5]) == [3, 4]
    assert plan_orchestration.find_ready_nodes(plan, [0, 1, 2], [3, 5]) == [4, 6]


def test_find_ready_nodes_rejects_inconsistent_state() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    with pytest.raises(plan_orchestration.PlanError, match="both done and running"):
        plan_orchestration.find_ready_nodes(plan, [0], [0])
    with pytest.raises(plan_orchestration.PlanError, match="no such nodes"):
        plan_orchestration.find_ready_nodes(plan, [42], [])


def test_render_node_task_carries_subtask_handoffs_and_report_path() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)
    report_path = Path("data/.tasks/build-app-parallel/todo/nodes/2/reports/report.md")

    text = plan_orchestration.render_node_task(
        plan=plan,
        node_idx=2,
        task_path=Path("data/.tasks/build-app-parallel/todo/nodes/2/task.md"),
        finish_report_path=report_path,
        report_by_node_idx={0: "Spec: items have a title.", 1: "Scaffolded todo on 8082."},
    )

    assert text.startswith(f"---\nfinish_report_path: {report_path}\n---\n")
    assert "This task file: `data/.tasks/build-app-parallel/todo/nodes/2/task.md`" in text
    assert "## Your subtask\n\nBuild the mock." in text
    assert "### Node 0\n\n**Its subtask:** Settle the spec." in text
    assert "Spec: items have a title." in text
    assert "Scaffolded todo on 8082." in text
    assert plan_orchestration.WORKER_RULES_REFERENCE in text


def test_render_node_task_without_dependencies_says_so() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    text = plan_orchestration.render_node_task(
        plan=plan,
        node_idx=0,
        task_path=Path("r/task.md"),
        finish_report_path=Path("r/report.md"),
        report_by_node_idx={},
    )

    assert "None. This node starts from the original request alone." in text


def test_render_node_task_refuses_interactive_and_missing_reports() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    with pytest.raises(plan_orchestration.PlanError, match="interactive"):
        plan_orchestration.render_node_task(
            plan=plan,
            node_idx=3,
            task_path=Path("t"),
            finish_report_path=Path("r"),
            report_by_node_idx={},
        )
    with pytest.raises(plan_orchestration.PlanError, match=r"nodes \[1\]"):
        plan_orchestration.render_node_task(
            plan=plan,
            node_idx=2,
            task_path=Path("t"),
            finish_report_path=Path("r"),
            report_by_node_idx={0: "spec"},
        )


def test_cli_parse_ready_and_write_task_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 0
    assert json.loads((run_dir / "plan.json").read_text())["nodes"][2]["access"] == [0, 1]

    capsys.readouterr()
    assert plan_orchestration.main(["ready", "--run-dir", str(run_dir), "--done", "0,1"]) == 0
    assert capsys.readouterr().out.strip() == "2,4"

    for idx, report in ((0, "Spec is settled."), (1, "Scaffolded.")):
        report_path = plan_orchestration.node_report_path(run_dir, idx)
        report_path.parent.mkdir(parents=True)
        report_path.write_text(report)
    assert (
        plan_orchestration.main(["write-task", "--run-dir", str(run_dir), "--node", "2"])
        == 0
    )
    task_text = plan_orchestration.node_task_path(run_dir, 2).read_text()
    assert f"finish_report_path: {run_dir / 'nodes' / '2' / 'reports' / 'report.md'}" in task_text
    assert "Spec is settled." in task_text


def test_cli_reports_plan_errors_with_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run_dir(tmp_path, "no plan here")

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 2
    assert "no <output>" in capsys.readouterr().err
    assert not (run_dir / "plan.json").exists()


def test_cli_write_task_before_dependencies_report_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)
    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 0

    assert (
        plan_orchestration.main(["write-task", "--run-dir", str(run_dir), "--node", "2"])
        == 2
    )
    assert "missing" in capsys.readouterr().err
    assert not plan_orchestration.node_task_path(run_dir, 2).exists()
