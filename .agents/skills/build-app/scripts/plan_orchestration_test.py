"""Tests for ``plan_orchestration.py``.

Run via: ``uv run pytest .agents/skills/build-app/scripts/plan_orchestration_test.py``
"""

from __future__ import annotations

import importlib.util
import json
import os
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


def test_each_capability_gets_its_own_model() -> None:
    assert plan_orchestration.MODEL_BY_CAPABILITY == {
        "low": "haiku",
        "medium": "sonnet[1m]",
        "high": "opus[1m]",
    }


def test_every_tier_asks_for_the_context_window_the_workspace_provisions() -> None:
    """A bare alias is accepted and quietly hands back a window smaller than the 1M the
    workspace pays for, and it reports the same display name either way -- so the only
    place that mistake can be caught is here. Haiku has no ``[1m]`` variant."""
    for capability, model in plan_orchestration.MODEL_BY_CAPABILITY.items():
        if model.startswith("haiku"):
            assert model == "haiku", capability
        else:
            assert model.endswith("[1m]"), capability


def test_no_tier_names_a_dated_model_id() -> None:
    """Every tier switches with an alias, which follows the pinned binary's own table to
    the current best model in that family. A dated id pins a tier to one release and
    goes stale silently on the next pin bump."""
    for capability, model in plan_orchestration.MODEL_BY_CAPABILITY.items():
        assert not model.startswith("claude-"), f"{capability} names {model}"


def test_an_interactive_node_has_no_model() -> None:
    assert plan_orchestration.model_for_capability("interactive") is None


def test_planner_prompt_examples_are_valid_plans() -> None:
    """The example plans the planner is shown must parse, so the prompt and the
    parser cannot drift apart."""
    prompt = (
        Path(__file__).parent.parent / "references" / "planner-prompt.md"
    ).read_text()
    example_blocks = re.findall(
        r"<output>\n(capability = .*?)\n</output>", prompt, re.DOTALL
    )

    assert example_blocks
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
            _plan_text(
                '["high", "low", "medium"]', '["a", "b", "c"]', "[[], [], [true]]"
            ),
            "access entry True",
        ),
        (
            _plan_text(
                '["high", "low", "medium"]', '["a", "b", "c"]', "[[], [], [0, 0]]"
            ),
            "same node twice",
        ),
        (
            _plan_text('["high", "low"]', '["a", "b"]', "[[], []]"),
            "2 nodes",
        ),
        (
            '<output>\ncapability = ["high"\n</output>',
            "not a valid JSON list",
        ),
        (
            '<output>\ncapability = ["high", "low", "medium"]\nsubtasks = ["a", "b", "c"]\n</output>',
            "no 'access list",
        ),
    ],
)
def test_parse_plan_rejects_malformed_plans(
    plan_text: str, message_fragment: str
) -> None:
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
    report_path = Path("data/.tasks/build-app/todo/nodes/2/reports/report.md")

    text = plan_orchestration.render_node_task(
        plan=plan,
        node_idx=2,
        finish_report_path=report_path,
        report_by_node_idx={
            0: "Spec: items have a title.",
            1: "Scaffolded todo on 8082.",
        },
    )

    assert text.startswith(f"---\nfinish_report_path: {report_path}\n---\n")
    assert (
        "## Your subtask\n\nBuild the mock.\n\nDo this subtask and nothing else."
        in text
    )
    assert "### Node 0\n\n**Its subtask:** Settle the spec." in text
    assert "Spec: items have a title." in text
    assert "Scaffolded todo on 8082." in text
    assert plan_orchestration.WORKER_RULES_REFERENCE in text


def test_render_node_task_orients_the_worker_before_it_starts() -> None:
    """The task says which node this is, where the file sits, and what the paths
    in it are relative to.

    Without this, four of six workers in one run spent their first commands
    running `find` and `ls` over the build folder hunting for a task file whose
    text they had already been handed, and two of them opened a sibling node's
    task on the way.
    """
    plan = plan_orchestration.parse_plan(_TODO_PLAN)
    report_path = Path("data/.tasks/build-app/todo/nodes/2/reports/report.md")

    text = plan_orchestration.render_node_task(
        plan=plan,
        node_idx=2,
        finish_report_path=report_path,
        report_by_node_idx={0: "Spec.", 1: "Scaffolded."},
    )

    assert "You are **node 2**." in text
    assert "data/.tasks/build-app/todo/nodes/2/task.md" in text
    assert "relative to the folder you are already" in text
    assert "not yours to read" in text


def test_render_node_task_without_dependencies_says_so() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    text = plan_orchestration.render_node_task(
        plan=plan,
        node_idx=0,
        finish_report_path=Path("r/report.md"),
        report_by_node_idx={},
    )

    assert "None. This node starts from the original request alone." in text


def test_read_node_report_prefers_the_live_report(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)
    report_path = plan_orchestration.node_report_path(run_dir, 0)
    report_path.parent.mkdir(parents=True)
    report_path.write_text("the live report")
    (report_path.parent / "consumed").mkdir()
    (report_path.parent / "consumed" / "20260101T000000Z-status-done.md").write_text(
        "older"
    )

    assert plan_orchestration.read_node_report(run_dir, 0) == "the live report"


def test_read_node_report_falls_back_to_the_archive(tmp_path: Path) -> None:
    """The launcher's poll archives a report as soon as it prints it, so by the time
    a dependent node's task is written the live path is usually empty."""
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)
    consumed = plan_orchestration.node_report_path(run_dir, 0).parent / "consumed"
    consumed.mkdir(parents=True)
    (consumed / "20260101T000000Z-status-done.md").write_text("the archived report")

    assert plan_orchestration.read_node_report(run_dir, 0) == "the archived report"


def test_read_node_report_takes_the_newest_archived_report(tmp_path: Path) -> None:
    """A worker that revises its work after a review delivers a fresh report."""
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)
    consumed = plan_orchestration.node_report_path(run_dir, 0).parent / "consumed"
    consumed.mkdir(parents=True)
    first = consumed / "20260101T000000Z-status-done.md"
    second = consumed / "20260102T000000Z-status-done.md"
    first.write_text("the first report")
    second.write_text("the revised report")
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))

    assert plan_orchestration.read_node_report(run_dir, 0) == "the revised report"


def test_read_node_report_is_none_when_the_node_never_delivered(tmp_path: Path) -> None:
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)

    assert plan_orchestration.read_node_report(run_dir, 0) is None


def test_cli_write_task_reads_an_archived_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the fallback: node 2's task is written from node 0's and
    node 1's reports after the poll has archived both."""
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)
    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 0
    for idx, body in ((0, "Spec is settled."), (1, "Scaffolded.")):
        consumed = plan_orchestration.node_report_path(run_dir, idx).parent / "consumed"
        consumed.mkdir(parents=True)
        (consumed / "20260101T000000Z-status-done.md").write_text(body)

    assert (
        plan_orchestration.main(
            ["write-task", "--run-dir", str(run_dir), "--node", "2"]
        )
        == 0
    )
    task_text = plan_orchestration.node_task_path(run_dir, 2).read_text()
    assert "Spec is settled." in task_text
    assert "Scaffolded." in task_text


def test_render_node_task_refuses_interactive_and_missing_reports() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    with pytest.raises(plan_orchestration.PlanError, match="interactive"):
        plan_orchestration.render_node_task(
            plan=plan,
            node_idx=3,
            finish_report_path=Path("r"),
            report_by_node_idx={},
        )
    with pytest.raises(plan_orchestration.PlanError, match=r"nodes \[1\]"):
        plan_orchestration.render_node_task(
            plan=plan,
            node_idx=2,
            finish_report_path=Path("r"),
            report_by_node_idx={0: "spec"},
        )


def test_cli_parse_ready_and_write_task_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run_dir(tmp_path, _TODO_PLAN)

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 0
    assert json.loads((run_dir / "plan.json").read_text())["nodes"][2]["access"] == [
        0,
        1,
    ]

    capsys.readouterr()
    assert (
        plan_orchestration.main(["ready", "--run-dir", str(run_dir), "--done", "0,1"])
        == 0
    )
    assert capsys.readouterr().out.strip() == "2,4"

    for idx, report in ((0, "Spec is settled."), (1, "Scaffolded.")):
        report_path = plan_orchestration.node_report_path(run_dir, idx)
        report_path.parent.mkdir(parents=True)
        report_path.write_text(report)
    assert (
        plan_orchestration.main(
            ["write-task", "--run-dir", str(run_dir), "--node", "2"]
        )
        == 0
    )
    task_text = plan_orchestration.node_task_path(run_dir, 2).read_text()
    assert (
        f"finish_report_path: {run_dir / 'nodes' / '2' / 'reports' / 'report.md'}"
        in task_text
    )
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
        plan_orchestration.main(
            ["write-task", "--run-dir", str(run_dir), "--node", "2"]
        )
        == 2
    )
    assert "missing" in capsys.readouterr().err
    assert not plan_orchestration.node_task_path(run_dir, 2).exists()


def test_cli_reports_missing_run_files_with_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run folder with no plan yet is a clear error, not a traceback."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 2
    assert "missing" in capsys.readouterr().err
    assert plan_orchestration.main(["ready", "--run-dir", str(run_dir)]) == 2
    assert "plan.json" in capsys.readouterr().err


def test_reduce_access_lists_drops_an_entry_a_sibling_already_reaches() -> None:
    # The shape every real plan so far has produced: the integration node names
    # the design node alongside the two nodes that built against it.
    reduced, dropped = plan_orchestration.reduce_access_lists(
        {0: [], 1: [], 2: [0, 1], 3: [0, 1], 4: [2], 5: [0, 2, 3, 4], 6: [5]}
    )

    assert reduced[5] == [3, 4]
    assert dropped[5] == [0, 2]
    # Nodes whose entries cover nothing between them are left alone.
    assert reduced[2] == [0, 1]
    assert dropped[2] == []
    assert reduced[6] == [5]


def test_reduce_access_lists_follows_a_chain_of_any_length() -> None:
    reduced, dropped = plan_orchestration.reduce_access_lists(
        {0: [], 1: [0], 2: [1], 3: [2], 4: [0, 1, 2, 3]}
    )

    assert reduced[4] == [3]
    assert dropped[4] == [0, 1, 2]


def test_reduce_access_lists_keeps_independent_entries() -> None:
    reduced, dropped = plan_orchestration.reduce_access_lists(
        {0: [], 1: [], 2: [], 3: [0, 1, 2]}
    )

    assert reduced[3] == [0, 1, 2]
    assert dropped == {0: [], 1: [], 2: [], 3: []}


def test_parse_plan_leaves_the_access_lists_alone_by_default() -> None:
    plan = plan_orchestration.parse_plan(_TODO_PLAN)

    assert [node["access"] for node in plan["nodes"]][5] == [3, 4]
    assert all(node["access_dropped"] == [] for node in plan["nodes"])


def test_parse_plan_reduces_the_access_lists_when_asked() -> None:
    plan_text = _plan_text(
        '["high", "medium", "medium", "high"]',
        '["Settle the spec.", "Scaffold.", "Build the store.", "Wire it up."]',
        "[[], [0], [0, 1], [0, 1, 2]]",
    )

    plan = plan_orchestration.parse_plan(plan_text, reduce_access=True)

    nodes = plan["nodes"]
    assert [node["access"] for node in nodes] == [[], [0], [1], [2]]
    assert [node["access_dropped"] for node in nodes] == [[], [], [0], [0, 1]]


def test_parse_plan_reduces_an_expanded_all_access_list() -> None:
    plan_text = _plan_text(
        '["high", "medium", "medium", "high"]',
        '["Settle the spec.", "Scaffold.", "Build the store.", "Wire it up."]',
        '[[], [0], [1], ["all"]]',
    )

    plan = plan_orchestration.parse_plan(plan_text, reduce_access=True)

    # ``["all"]`` expands to every earlier node, and all but the last are covered.
    assert plan["nodes"][3]["access"] == [2]
    assert plan["nodes"][3]["access_dropped"] == [0, 1]


def test_parse_command_says_which_dependencies_it_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_text = _plan_text(
        '["high", "medium", "medium", "high"]',
        '["Settle the spec.", "Scaffold.", "Build the store.", "Wire it up."]',
        "[[], [0], [0, 1], [0, 1, 2]]",
    )
    run_dir = _write_run_dir(tmp_path, plan_text)

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir), "--reduce-access"]) == 0

    out = capsys.readouterr().out
    assert "node 3 no longer waits on [0, 1]" in out
    assert "([2]) already depend on them" in out
    written = json.loads((run_dir / "plan.json").read_text())
    assert [node["access"] for node in written["nodes"]] == [[], [0], [1], [2]]


def test_parse_command_leaves_the_plan_alone_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_text = _plan_text(
        '["high", "medium", "medium", "high"]',
        '["Settle the spec.", "Scaffold.", "Build the store.", "Wire it up."]',
        "[[], [0], [0, 1], [0, 1, 2]]",
    )
    run_dir = _write_run_dir(tmp_path, plan_text)

    assert plan_orchestration.main(["parse", "--run-dir", str(run_dir)]) == 0

    assert "no longer waits on" not in capsys.readouterr().out
    written = json.loads((run_dir / "plan.json").read_text())
    assert [node["access"] for node in written["nodes"]] == [[], [0], [0, 1], [0, 1, 2]]


# The DAG this flag was designed against: one node opens the build, three run at once off
# it, one joins them back, one finishes. Only the middle three are worth a worker.
_FAN_OUT_ACCESS = "[[], [0], [0], [0], [1, 2, 3], [4]]"
_FAN_OUT_PLAN = _plan_text(
    '["high", "medium", "medium", "medium", "high", "low"]',
    '["open", "a", "b", "c", "join", "finish"]',
    _FAN_OUT_ACCESS,
)


def test_waves_replay_the_order_the_ready_loop_would_take() -> None:
    access = {0: [], 1: [0], 2: [0], 3: [0], 4: [1, 2, 3], 5: [4]}
    assert plan_orchestration.schedule_waves(access) == [[0], [1, 2, 3], [4], [5]]


def test_a_wave_is_capped_at_the_parallelism_limit() -> None:
    """Six nodes with nothing to wait for are reported as five then one.

    This pins the known inaccuracy `schedule_waves` documents rather than endorsing it:
    the live loop starts the sixth as soon as a slot frees, so it really does run beside
    the others, and `--only-parallel-workers` takes its worker away anyway. Accepted
    because it needs six nodes unblocked at once, and because a node running with four
    others already has every slot the cap allows."""
    access = {idx: [] for idx in range(6)}
    waves = plan_orchestration.schedule_waves(access)
    assert waves == [[0, 1, 2, 3, 4], [5]]
    assert len(waves[0]) == plan_orchestration.MAX_RUNNING_NODE_COUNT


def test_waves_refuse_a_cycle() -> None:
    with pytest.raises(plan_orchestration.PlanError, match="cycle"):
        plan_orchestration.schedule_waves({0: [1], 1: [0]})


def test_only_parallel_workers_leaves_the_solo_nodes_to_the_orchestrator() -> None:
    """The flag's whole purpose, on the DAG it was designed against."""
    nodes = plan_orchestration.parse_plan(_FAN_OUT_PLAN, only_parallel_workers=True)[
        "nodes"
    ]
    assert [node["index"] for node in nodes if node["has_worker"]] == [1, 2, 3]
    assert [node["index"] for node in nodes if not node["has_worker"]] == [0, 4, 5]


def test_off_by_default_every_node_still_gets_a_worker() -> None:
    """The flag off is the flow as it has always run, so nothing moves without it."""
    nodes = plan_orchestration.parse_plan(_FAN_OUT_PLAN)["nodes"]
    assert all(node["has_worker"] for node in nodes)


def test_a_node_with_no_worker_has_no_model() -> None:
    """A model is what a worker is launched with, so a node without one asks for none."""
    nodes = plan_orchestration.parse_plan(_FAN_OUT_PLAN, only_parallel_workers=True)[
        "nodes"
    ]
    for node in nodes:
        assert (node["model"] is not None) == node["has_worker"], node["index"]


def test_an_interactive_node_never_counts_as_company() -> None:
    """An interactive node is the orchestrator talking, so it cannot be building
    something else at the same time -- a worker node beside one is still alone."""
    plan = _plan_text(
        '["high", "interactive", "medium"]',
        '["build", "ask the user", "finish"]',
        "[[], [], [0, 1]]",
    )
    nodes = plan_orchestration.parse_plan(plan, only_parallel_workers=True)["nodes"]
    assert not any(node["has_worker"] for node in nodes)


def test_a_node_the_orchestrator_runs_takes_no_worker_slot() -> None:
    """A node with no worker must not consume one of the five worker slots: it is the
    orchestrator's own turn, not an agent occupying a slot."""
    # Six nodes waiting on nothing: five fill the cap and get workers, the sixth is
    # alone in the next wave and so is the orchestrator's.
    plan = plan_orchestration.parse_plan(
        _plan_text(
            '["high", "high", "high", "high", "high", "high"]',
            '["a", "b", "c", "d", "e", "f"]',
            "[[], [], [], [], [], []]",
        ),
        only_parallel_workers=True,
    )
    assert [node["index"] for node in plan["nodes"] if not node["has_worker"]] == [5]
    # All five slots are busy, and node 5 is still offered because it needs none.
    assert plan_orchestration.find_ready_nodes(plan, [], [0, 1, 2, 3, 4]) == [5]


def test_a_plan_written_before_the_flag_still_schedules() -> None:
    """find_ready_nodes reads plan.json from disk, and a build in flight when this
    shipped has nodes with no `has_worker` key at all."""
    plan = plan_orchestration.parse_plan(_FAN_OUT_PLAN)
    for node in plan["nodes"]:
        del node["has_worker"]
    assert plan_orchestration.find_ready_nodes(plan, [0], []) == [1, 2, 3]


def test_consecutive_orchestrator_nodes_become_one_piece_of_work() -> None:
    """Nodes 4 and 5 run back to back with nobody else involved, so splitting them
    divides work between one agent and itself."""
    plan = plan_orchestration.parse_plan(_FAN_OUT_PLAN, only_parallel_workers=True)
    assert plan["own_groups"] == [[0], [4, 5]]
    assert [node["own_group"] for node in plan["nodes"]] == [0, None, None, None, 1, 1]


def test_an_interactive_node_stays_inside_a_run() -> None:
    """An interactive node is the same agent asking a question it then acts on, so it
    does not divide the work -- it only fixes an order inside the run."""
    plan = plan_orchestration.parse_plan(
        _plan_text(
            '["high", "medium", "medium", "interactive", "high", "low"]',
            '["open", "a", "b", "ask the user", "join", "finish"]',
            "[[], [0], [0], [1, 2], [3], [4]]",
        ),
        only_parallel_workers=True,
    )
    # 1 and 2 run together so they keep their workers; everything after is one run.
    assert plan["own_groups"] == [[0], [3, 4, 5]]


def test_a_worker_node_ends_a_run_of_the_orchestrators_own() -> None:
    """The orchestrator has to wait for a worker and merge its branch, so its own work
    cannot continue across one."""
    waves = [[0], [1, 2], [3]]
    groups = plan_orchestration.group_orchestrator_nodes(waves, {1, 2})
    assert groups == [[0], [3]]


def test_with_the_flag_off_the_orchestrator_owns_only_interactive_nodes() -> None:
    plan = plan_orchestration.parse_plan(
        _plan_text(
            '["high", "interactive", "medium"]',
            '["build", "ask", "finish"]',
            "[[], [], [0, 1]]",
        )
    )
    assert plan["own_groups"] == [[1]]


def test_every_orchestrator_node_lands_in_exactly_one_group() -> None:
    """A node the orchestrator owns but no group names would silently never be done."""
    plan = plan_orchestration.parse_plan(_FAN_OUT_PLAN, only_parallel_workers=True)
    grouped = [idx for group in plan["own_groups"] for idx in group]
    assert sorted(grouped) == sorted(
        node["index"] for node in plan["nodes"] if not node["has_worker"]
    )
    assert len(grouped) == len(set(grouped))


def test_a_worker_after_an_orchestrator_node_needs_that_node_reported(
    tmp_path: Path,
) -> None:
    """The flag's own DAG, end to end. Nodes 1-3 wait on node 0, which the orchestrator does
    itself -- so their task files quote node 0's report, and `write-task` fails until the
    orchestrator has written one. That failure is the flag's sharpest edge: a node done but
    left unreported blocks every node depending on it."""
    plan = plan_orchestration.parse_plan(_FAN_OUT_PLAN, only_parallel_workers=True)
    assert not plan["nodes"][0]["has_worker"]

    report_path = tmp_path / "nodes" / "1" / "reports" / "report.md"
    with pytest.raises(plan_orchestration.PlanError, match="has_worker false"):
        plan_orchestration.render_node_task(plan, 1, report_path, {})

    # Once the orchestrator reports its own node, the dependent worker's task renders.
    task = plan_orchestration.render_node_task(
        plan, 1, report_path, {0: "Set up the package and the data loader."}
    )
    assert "Set up the package and the data loader." in task
    assert "### Node 0" in task
