#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic plumbing for carrying out a build-app-parallel plan.

The planner writes a plan as three parallel lists inside an ``<output>`` tag:
``capability``, ``subtasks`` and ``access list``, one entry per node. The
orchestrating agent keeps the judgment (reading reports, talking to the user,
deciding what to do when a node goes wrong); this script owns the parts with a
single right answer.

Every command works on one run folder, ``data/.tasks/build-app-parallel/<slug>/``:

    plan.md                         the planner's raw output
    plan.json                       the validated plan (written by ``parse``)
    nodes/<index>/task.md           a node's worker task (written by ``write-task``)
    nodes/<index>/reports/report.md where that node's worker delivers its report

Three subcommands:

``parse``
    Extract the three lists from ``plan.md``, validate them, expand ``["all"]``
    access lists, attach the model each node's worker runs on, and write
    ``plan.json``. Exits 2 with a message naming the problem when the plan is
    malformed.

``ready``
    Given which nodes are done and which are running, print the nodes that can
    start now: every node in their access list is done, and starting them keeps
    the number of running workers at or under the parallelism cap. Interactive
    nodes are included and take no worker slot; the orchestrator runs those
    itself rather than launching a worker.

``write-task``
    Write one node's worker task file: frontmatter naming where its report must
    land, its subtask, and the subtask and report of every node in its access
    list. The standing rules every node follows live in
    ``references/worker-node.md``, which the task file points at.

Run with bare ``python3``: standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final, Sequence

# The capabilities a planner may assign. ``interactive`` nodes are run by the
# orchestrating agent with the user and never get a worker.
CAPABILITIES: Final[tuple[str, ...]] = ("low", "medium", "high", "interactive")
INTERACTIVE_CAPABILITY: Final[str] = "interactive"

# The model a worker runs on, by capability. Every capability starts on Opus;
# this table is the one place to change that.
MODEL_BY_CAPABILITY: Final[dict[str, str]] = {
    "low": "opus",
    "medium": "opus",
    "high": "opus",
}

MIN_NODE_COUNT: Final[int] = 3
MAX_NODE_COUNT: Final[int] = 15
MAX_RUNNING_NODE_COUNT: Final[int] = 5

# The planner's list names, in the order they appear in its output.
_CAPABILITY_LIST_NAME: Final[str] = "capability"
_SUBTASKS_LIST_NAME: Final[str] = "subtasks"
_ACCESS_LIST_NAME: Final[str] = "access list"
_ALL_ACCESS_ENTRY: Final[str] = "all"

_OUTPUT_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<output>(.*?)</output>", re.DOTALL
)

WORKER_RULES_REFERENCE: Final[str] = (
    ".agents/skills/build-app-parallel/references/worker-node.md"
)
PLAN_MARKDOWN_FILE_NAME: Final[str] = "plan.md"
PLAN_JSON_FILE_NAME: Final[str] = "plan.json"


def node_folder(run_dir: Path, node_idx: int) -> Path:
    return run_dir / "nodes" / str(node_idx)


def node_task_path(run_dir: Path, node_idx: int) -> Path:
    return node_folder(run_dir, node_idx) / "task.md"


def node_report_path(run_dir: Path, node_idx: int) -> Path:
    return node_folder(run_dir, node_idx) / "reports" / "report.md"


def read_node_report(run_dir: Path, node_idx: int) -> str | None:
    """A finished node's report, wherever it currently sits, or ``None``.

    The launcher's ``await`` archives a report into ``reports/consumed/`` under a
    timestamped name the moment it prints it, so by the time a dependent node's
    task is written the live path is usually empty. Reading the archive too is
    what keeps a handoff from going missing between the poll and the next launch;
    the newest archived report wins, since a worker that revises its work after a
    review delivers a fresh one.
    """
    live_path = node_report_path(run_dir, node_idx)
    if live_path.is_file():
        return live_path.read_text(encoding="utf-8")
    archived = sorted(
        (live_path.parent / "consumed").glob("*.md"),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not archived:
        return None
    return archived[-1].read_text(encoding="utf-8")


class PlanError(ValueError):
    """Raised when a planner's output is not a usable plan."""


def _extract_output_block(plan_text: str) -> str:
    """The last ``<output>`` block: the plan follows the planner's reasoning, which
    may itself mention the tag."""
    matches = _OUTPUT_TAG_PATTERN.findall(plan_text)
    if not matches:
        raise PlanError("the plan has no <output>...</output> block")
    return matches[-1]


def _extract_list(output_block: str, list_name: str) -> list[object]:
    """Decode the JSON list that follows ``<list_name> =`` in the output block."""
    name_match = re.search(
        rf"^\s*{re.escape(list_name)}\s*=\s*", output_block, re.MULTILINE
    )
    if name_match is None:
        raise PlanError(f"the plan has no '{list_name} = [...]' line")
    try:
        value, _end = json.JSONDecoder().raw_decode(output_block, name_match.end())
    except json.JSONDecodeError as e:
        raise PlanError(f"'{list_name}' is not a valid JSON list: {e}") from None
    if not isinstance(value, list):
        raise PlanError(f"'{list_name}' must be a list")
    return value


def _validate_capabilities(capabilities: Sequence[object]) -> list[str]:
    for idx, capability in enumerate(capabilities):
        if capability not in CAPABILITIES:
            raise PlanError(
                f"node {idx} has capability {capability!r}; expected one of {CAPABILITIES}"
            )
    return [str(capability) for capability in capabilities]


def _validate_subtasks(subtasks: Sequence[object]) -> list[str]:
    for idx, subtask in enumerate(subtasks):
        if not isinstance(subtask, str) or not subtask.strip():
            raise PlanError(f"node {idx} has an empty or non-string subtask")
    return [str(subtask) for subtask in subtasks]


def _expand_access_list(node_idx: int, access: object) -> list[int]:
    """Validate one node's access list and return the earlier node indices it names."""
    if not isinstance(access, list):
        raise PlanError(f"node {node_idx} has a non-list access list: {access!r}")
    if access == [_ALL_ACCESS_ENTRY]:
        return list(range(node_idx))
    for entry in access:
        # bool is an int subclass; a planner writing true/false is a malformed plan.
        is_index = isinstance(entry, int) and not isinstance(entry, bool)
        if not is_index or not 0 <= entry < node_idx:
            raise PlanError(
                f"node {node_idx} has access entry {entry!r}; entries must be earlier "
                f"node indices (0 to {node_idx - 1}), or the list must be exactly ['all']"
            )
    if len(set(access)) != len(access):
        raise PlanError(f"node {node_idx} lists the same node twice: {access!r}")
    return sorted(int(entry) for entry in access)


def parse_plan(plan_text: str) -> dict[str, object]:
    """Turn the planner's output into the validated plan the orchestrator runs."""
    output_block = _extract_output_block(plan_text)
    capabilities = _validate_capabilities(
        _extract_list(output_block, _CAPABILITY_LIST_NAME)
    )
    subtasks = _validate_subtasks(_extract_list(output_block, _SUBTASKS_LIST_NAME))
    access_lists = _extract_list(output_block, _ACCESS_LIST_NAME)

    # All three lists describe the same nodes, position by position.
    lengths = {len(capabilities), len(subtasks), len(access_lists)}
    if len(lengths) != 1:
        raise PlanError(
            f"the lists differ in length: capability={len(capabilities)}, "
            f"subtasks={len(subtasks)}, access list={len(access_lists)}"
        )
    node_count = len(capabilities)
    if not MIN_NODE_COUNT <= node_count <= MAX_NODE_COUNT:
        raise PlanError(
            f"the plan has {node_count} nodes; expected {MIN_NODE_COUNT} to {MAX_NODE_COUNT}"
        )

    nodes = [
        {
            "index": idx,
            "capability": capabilities[idx],
            "subtask": subtasks[idx],
            "access": _expand_access_list(idx, access_lists[idx]),
            "model": MODEL_BY_CAPABILITY.get(capabilities[idx]),
        }
        for idx in range(node_count)
    ]
    return {"nodes": nodes}


def find_ready_nodes(
    plan: dict[str, object],
    done_node_indices: Sequence[int],
    running_node_indices: Sequence[int],
) -> list[int]:
    """Nodes that can start now, lowest index first.

    Only worker nodes count toward the parallelism cap: an interactive node is a
    conversation the orchestrator holds, so it starts whenever its dependencies
    are done and takes no worker slot.
    """
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    done = set(done_node_indices)
    running = set(running_node_indices)
    overlap = done & running
    if overlap:
        raise PlanError(f"nodes listed as both done and running: {sorted(overlap)}")
    unknown = (done | running) - set(range(len(nodes)))
    if unknown:
        raise PlanError(f"no such nodes in the plan: {sorted(unknown)}")

    def is_interactive(node_idx: int) -> bool:
        return nodes[node_idx]["capability"] == INTERACTIVE_CAPABILITY

    unblocked = [
        node["index"]
        for node in nodes
        if node["index"] not in done
        and node["index"] not in running
        and set(node["access"]) <= done
    ]
    running_worker_count = sum(1 for idx in running if not is_interactive(idx))
    free_worker_slot_count = max(MAX_RUNNING_NODE_COUNT - running_worker_count, 0)
    ready_workers = [idx for idx in unblocked if not is_interactive(idx)][
        :free_worker_slot_count
    ]
    ready_interactive = [idx for idx in unblocked if is_interactive(idx)]
    return sorted(ready_workers + ready_interactive)


def render_node_task(
    plan: dict[str, object],
    node_idx: int,
    finish_report_path: Path,
    report_by_node_idx: dict[int, str],
) -> str:
    """The task file for one node's worker, with the handoffs it has access to."""
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    if not 0 <= node_idx < len(nodes):
        raise PlanError(f"no such node in the plan: {node_idx}")
    node = nodes[node_idx]
    if node["capability"] == INTERACTIVE_CAPABILITY:
        raise PlanError(
            f"node {node_idx} is interactive; the orchestrator runs it, not a worker"
        )
    missing_reports = [idx for idx in node["access"] if idx not in report_by_node_idx]
    if missing_reports:
        raise PlanError(
            f"node {node_idx} needs the reports of nodes {missing_reports}, which are "
            f"missing from both reports/report.md and reports/consumed/ -- those nodes "
            f"have not delivered, so this node is not ready to start"
        )

    handoff_sections = [
        f"### Node {idx}\n\n**Its subtask:** {nodes[idx]['subtask']}\n\n"
        f"**What it handed back:**\n\n{report_by_node_idx[idx].strip()}\n"
        for idx in node["access"]
    ]
    handoffs = (
        "\n".join(handoff_sections)
        if handoff_sections
        else "None. This node starts from the original request alone.\n"
    )
    return (
        f"---\nfinish_report_path: {finish_report_path}\n---\n\n"
        f"# Task: node {node_idx} of an app build\n\n"
        f"Follow `{WORKER_RULES_REFERENCE}` for how to work in the shared build "
        "folder and how to report back. It is part of this task.\n\n"
        f"## Your subtask\n\n{node['subtask']}\n\n"
        "Do this subtask and nothing else. Other nodes are building the rest of "
        "the app, some of them in this folder right now.\n\n"
        f"## Handoffs from the nodes you depend on\n\n{handoffs}"
    )


def _read_run_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlanError(f"missing {path}") from None


def _read_plan_json(run_dir: Path) -> dict[str, object]:
    return json.loads(_read_run_file(run_dir / PLAN_JSON_FILE_NAME))


def _parse_node_index_list(text: str) -> list[int]:
    """Parse a comma-separated index list such as ``0,2,5`` (empty means none)."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [int(part) for part in stripped.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected comma-separated node indices, got {text!r}"
        ) from None


def _run_parse(run_dir: Path) -> int:
    plan = parse_plan(_read_run_file(run_dir / PLAN_MARKDOWN_FILE_NAME))
    plan_json_path = run_dir / PLAN_JSON_FILE_NAME
    plan_json_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(f"plan_orchestration: wrote {len(plan['nodes'])} nodes to {plan_json_path}")
    return 0


def _run_ready(
    run_dir: Path, done_node_indices: Sequence[int], running_node_indices: Sequence[int]
) -> int:
    ready = find_ready_nodes(
        _read_plan_json(run_dir), done_node_indices, running_node_indices
    )
    print(",".join(str(idx) for idx in ready))
    return 0


def _run_write_task(run_dir: Path, node_idx: int) -> int:
    plan = _read_plan_json(run_dir)
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    if not 0 <= node_idx < len(nodes):
        raise PlanError(f"no such node in the plan: {node_idx}")
    report_by_node_idx = {
        idx: report
        for idx in nodes[node_idx]["access"]
        if (report := read_node_report(run_dir, idx)) is not None
    }
    task_path = node_task_path(run_dir, node_idx)
    task_text = render_node_task(
        plan=plan,
        node_idx=node_idx,
        finish_report_path=node_report_path(run_dir, node_idx),
        report_by_node_idx=report_by_node_idx,
    )
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(task_text, encoding="utf-8")
    print(f"plan_orchestration: wrote the task for node {node_idx} to {task_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser(
        "parse", help="Validate <run-dir>/plan.md and write <run-dir>/plan.json."
    )
    parse_parser.add_argument("--run-dir", type=Path, required=True)

    ready_parser = subparsers.add_parser(
        "ready", help="Print the nodes that can start now, comma-separated."
    )
    ready_parser.add_argument("--run-dir", type=Path, required=True)
    ready_parser.add_argument(
        "--done", type=_parse_node_index_list, default=[], help="e.g. 0,1,3"
    )
    ready_parser.add_argument(
        "--running", type=_parse_node_index_list, default=[], help="e.g. 2,4"
    )

    task_parser = subparsers.add_parser(
        "write-task", help="Write <run-dir>/nodes/<node>/task.md for one node's worker."
    )
    task_parser.add_argument("--run-dir", type=Path, required=True)
    task_parser.add_argument("--node", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        match args.command:
            case "parse":
                return _run_parse(args.run_dir)
            case "ready":
                return _run_ready(args.run_dir, args.done, args.running)
            case "write-task":
                return _run_write_task(args.run_dir, args.node)
            case _:
                raise PlanError(f"unknown command: {args.command}")
    except PlanError as e:
        print(f"plan_orchestration: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
