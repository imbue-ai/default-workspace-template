import json

import pytest

from imbue.minds_evals.data_types import DeciderTurn
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.errors import TrajectoryDocumentError
from imbue.minds_evals.testing import WORKER_AGENT_ID
from imbue.minds_evals.testing import WORKER_LAUNCH_CALL_ID
from imbue.minds_evals.testing import WORKER_LAUNCH_COMMAND
from imbue.minds_evals.testing import WORKER_NAME
from imbue.minds_evals.testing import WORKER_TASK_FILE
from imbue.minds_evals.testing import atif_document
from imbue.minds_evals.testing import atif_document_json
from imbue.minds_evals.testing import atif_document_with_worker_launch
from imbue.minds_evals.testing import worker_document
from imbue.minds_evals.testing import worker_stream_jsonl
from imbue.minds_evals.trajectory import EmbeddedWorker
from imbue.minds_evals.trajectory import build_hand_built_trajectory
from imbue.minds_evals.trajectory import build_worker_trajectory_from_stream
from imbue.minds_evals.trajectory import build_workspace_trajectory
from imbue.minds_evals.trajectory import graft_worker_trajectories
from imbue.minds_evals.trajectory import parse_worker_document
from imbue.minds_evals.trajectory import scan_worker_launches
from imbue.minds_evals.usage import TrialUsage
from imbue.mngr_usage.data_types import TokenSnapshot


def _provenance() -> TrajectoryProvenance:
    return TrajectoryProvenance(
        driver_name="minds-persona-driver",
        driver_version="0.1.0",
        decider_model="claude-opus-4-8",
        decider_turns=(
            DeciderTurn(
                turn=2,
                entry_index=1,
                exchange=0,
                entry_kind=TurnEntryKind.PERSONA,
                model="claude-opus-4-8",
                is_fallback=False,
                detail="",
            ),
        ),
        harbor_session_id="session-1",
        case_id="todo-app",
        usage_source=UsageSource.PROXY,
    )


def _usage(message_count: int) -> TrialUsage:
    return TrialUsage(
        per_model=(),
        tokens=TokenSnapshot(input=10, output=5, cache_read=100, cache_creation=20),
        cost_usd=0.25,
        message_count=message_count,
        unpriced_models=(),
        delegated_call_count=0,
        worker_launch_count=0,
    )


_EXPECTED_EXTRA = {
    "driver": {"name": "minds-persona-driver", "version": "0.1.0"},
    "decider_model": "claude-opus-4-8",
    "decider_turns": [
        {
            "turn": 2,
            "entry_index": 1,
            "exchange": 0,
            "entry_kind": "persona",
            "model": "claude-opus-4-8",
            "is_fallback": False,
            "detail": "",
        }
    ],
    "harbor_session_id": "session-1",
    "case_id": "todo-app",
    "usage_source": "proxy",
}


def test_workspace_trajectory_carries_the_resolved_usage_and_leaves_the_rest_alone() -> None:
    document = atif_document()

    built = build_workspace_trajectory(
        atif_document_json(), _provenance(), _usage(message_count=2), workers=()
    ).to_json_dict()

    # The trial's resolved account replaces the document's own per-step sums, cache-inclusive as ATIF
    # defines prompt tokens, while the step count stays the document's.
    assert built["final_metrics"] == {
        "total_prompt_tokens": 130,
        "total_completion_tokens": 5,
        "total_cached_tokens": 100,
        "total_cost_usd": 0.25,
        "total_steps": 2,
    }
    assert built["extra"] == {"workspace_note": "kept", "minds_evals": {"source": "workspace", **_EXPECTED_EXTRA}}
    # Everything the workspace wrote survives: identities, steps with their observations and
    # provenance, and the embedded subagent.
    for field in ("schema_version", "session_id", "trajectory_id", "agent", "steps", "subagent_trajectories"):
        assert built[field] == document[field], field


def test_workspace_trajectory_keeps_the_documents_own_sums_when_the_usage_account_is_empty() -> None:
    built = build_workspace_trajectory(
        atif_document_json(), _provenance(), _usage(message_count=0), workers=()
    ).to_json_dict()

    assert built["final_metrics"] == atif_document()["final_metrics"]


def test_workspace_trajectory_is_validated_after_the_edits() -> None:
    stepless = {**atif_document(), "steps": []}

    with pytest.raises(TrajectoryDocumentError, match="not valid ATIF"):
        build_workspace_trajectory(json.dumps(stepless), _provenance(), _usage(message_count=2), workers=())


@pytest.mark.parametrize("document_json", ["{not json", "[1, 2]"])
def test_workspace_trajectory_refuses_a_document_that_is_not_an_object(document_json: str) -> None:
    with pytest.raises(TrajectoryDocumentError):
        build_workspace_trajectory(document_json, _provenance(), _usage(message_count=2), workers=())


def test_hand_built_trajectory_carries_the_same_provenance_block_and_skips_empty_turns() -> None:
    conversation = [
        {"role": "user", "text": "Build it"},
        {"role": "agent", "text": ""},
        {"role": "user", "text": "Sounds good."},
        {"role": "agent", "text": "Done."},
    ]

    built = build_hand_built_trajectory(
        conversation, _provenance(), _usage(message_count=2), timestamp="2026-09-01T00:00:00Z"
    )

    assert built is not None
    rendered = built.to_json_dict()
    assert [(step["step_id"], step["source"], step["message"]) for step in rendered["steps"]] == [
        (1, "user", "Build it"),
        (2, "user", "Sounds good."),
        (3, "agent", "Done."),
    ]
    # The decider is described in the provenance block, never as the agent's model.
    assert rendered["agent"] == {"name": "minds-persona-driver", "version": "0.1.0"}
    assert rendered["session_id"] == "session-1"
    assert rendered["extra"] == {"minds_evals": {"source": "hand_built", **_EXPECTED_EXTRA}}
    assert rendered["final_metrics"]["total_steps"] == 3
    assert rendered["final_metrics"]["total_prompt_tokens"] == 130


def test_hand_built_trajectory_is_none_without_an_exchange() -> None:
    assert (
        build_hand_built_trajectory(
            [{"role": "user", "text": "   "}], _provenance(), _usage(message_count=0), timestamp="t"
        )
        is None
    )


# --- background workers ---


def _launch(name: str = WORKER_NAME, depth: int = 0, lead_name: str = "") -> WorkerLaunch:
    return WorkerLaunch(
        name=name, tool_call_id=WORKER_LAUNCH_CALL_ID, task_file=WORKER_TASK_FILE, depth=depth, lead_name=lead_name
    )


def _bash_step(step_id: int, command: str, call_id: str) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "tool_calls": [{"tool_call_id": call_id, "function_name": "Bash", "arguments": {"command": command}}],
    }


def test_scan_worker_launches_finds_each_launch_once_and_ignores_the_rest() -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": WORKER_LAUNCH_COMMAND},
        _bash_step(2, WORKER_LAUNCH_COMMAND, "c1"),
        _bash_step(
            3, "uv run .agents/skills/launch-task/scripts/create_worker.py await --name crystallize-todo", "c2"
        ),
        _bash_step(4, "cd x && mngr create side-quest -t worker", "c3"),
        _bash_step(5, "uv run create_worker.py launch-sync --task-file t.md --name sync-worker", "c4"),
        _bash_step(6, WORKER_LAUNCH_COMMAND, "c5"),
        _bash_step(7, "uv run create_worker.py destroy --name side-quest", "c6"),
        _bash_step(8, "uv run create_worker.py launch --name=equals --task-file=e.md", "c7"),
    ]

    launches = scan_worker_launches(steps, depth=1, lead_name="lead")

    assert [(launch.name, launch.tool_call_id, launch.task_file) for launch in launches] == [
        (WORKER_NAME, "c1", WORKER_TASK_FILE),
        ("side-quest", "c3", ""),
        ("sync-worker", "c4", "t.md"),
        ("equals", "c7", "e.md"),
    ]
    assert {(launch.depth, launch.lead_name) for launch in launches} == {(1, "lead")}


def test_scan_worker_launches_reads_a_stream_as_well_as_a_document() -> None:
    records = [
        {"type": "header"},
        {
            "type": "step",
            "source": "agent",
            "tool_calls": [{"tool_call_id": "c1", "arguments": {"command": WORKER_LAUNCH_COMMAND}}],
        },
        {"type": "observation", "results": []},
    ]

    assert [launch.name for launch in scan_worker_launches(records, depth=0, lead_name="")] == [WORKER_NAME]


def test_scan_worker_launches_reads_the_launch_as_the_skill_spells_it() -> None:
    # The skill's snippet: a variable for the name, the options on backslash-continued lines, and
    # the launch followed by more commands in the same call.
    as_documented = (
        "NAME=crystallize-todo\n"
        "uv run .agents/skills/launch-task/scripts/create_worker.py launch \\\n"
        "    --name $NAME \\\n"
        "    --template worker \\\n"
        '    --runtime-dir "data/.tasks/harden/$NAME/" \\\n'
        '    --task-file "data/.tasks/harden/$NAME/task.md"\n'
        "echo launched"
    )
    steps = [
        _bash_step(1, as_documented, "c1"),
        _bash_step(2, "export W='braced'; mngr create ${W} -t worker", "c2"),
        _bash_step(3, "uv run create_worker.py launch --name $UNSET --task-file t.md", "c3"),
        _bash_step(4, "uv run create_worker.py launch --name $LATE --task-file t.md\nLATE=too-late", "c4"),
        _bash_step(
            5,
            "NAME=nested\nTASK_DIR=data/.tasks/harden/$NAME\n"
            "uv run create_worker.py launch --name $NAME --task-file $TASK_DIR/task.md",
            "c5",
        ),
    ]

    launches = scan_worker_launches(steps, depth=0, lead_name="")

    # The variable is resolved wherever it appears, in the name and inside the task-file path, through
    # an assignment that itself used an earlier one; one the command never assigned, or assigns only
    # after the launch, stays as written, so the launch is still counted.
    assert [(launch.name, launch.task_file) for launch in launches] == [
        (WORKER_NAME, WORKER_TASK_FILE),
        ("braced", ""),
        ("$UNSET", "t.md"),
        ("$LATE", "t.md"),
        ("nested", "data/.tasks/harden/nested/task.md"),
    ]


def test_graft_embeds_the_worker_under_its_launching_call() -> None:
    document = atif_document_with_worker_launch()
    worker = EmbeddedWorker(
        launch=_launch(), document=worker_document(WORKER_AGENT_ID), state=WorkerState.STOPPED, report_path="r"
    )

    grafted = graft_worker_trajectories(document, [worker])

    launch_step = grafted["steps"][2]
    assert launch_step["observation"]["results"][0]["subagent_trajectory_ref"] == [
        {"trajectory_id": WORKER_AGENT_ID, "extra": {"subagent_kind": "mngr", "worker_name": WORKER_NAME}}
    ]
    # The launch's own output stays as the quick-reference summary.
    assert launch_step["observation"]["results"][0]["content"] == "Creating agent state... Done."
    embedded = grafted["subagent_trajectories"]
    assert [entry["trajectory_id"] for entry in embedded] == ["sub-1", WORKER_AGENT_ID]
    assert embedded[1]["extra"]["subagent_kind"] == "mngr"
    assert embedded[1]["extra"]["worker"] == {
        "name": WORKER_NAME,
        "agent_id": WORKER_AGENT_ID,
        "state": "stopped",
        "lead_agent_id": "chat-1",
        "launch_tool_call_id": WORKER_LAUNCH_CALL_ID,
        "report_path": "r",
    }
    # Every other step is untouched, and the original document was not mutated.
    assert grafted["steps"][:2] == document["steps"][:2]
    assert "subagent_trajectory_ref" not in document["steps"][2]["observation"]["results"][0]


def test_graft_synthesizes_a_pending_result_when_the_launch_has_no_output() -> None:
    document = atif_document_with_worker_launch()
    document["steps"][2].pop("observation")
    worker = EmbeddedWorker(
        launch=_launch(), document=worker_document(WORKER_AGENT_ID), state=WorkerState.STOPPED, report_path=""
    )

    grafted = graft_worker_trajectories(document, [worker])

    assert grafted["steps"][2]["observation"]["results"] == [
        {
            "source_call_id": WORKER_LAUNCH_CALL_ID,
            "content": None,
            "subagent_trajectory_ref": [
                {"trajectory_id": WORKER_AGENT_ID, "extra": {"subagent_kind": "mngr", "worker_name": WORKER_NAME}}
            ],
            "extra": {"subagent_result_pending": True},
        }
    ]


def test_graft_still_embeds_a_worker_whose_launching_call_is_missing() -> None:
    worker = EmbeddedWorker(
        launch=_launch(), document=worker_document(WORKER_AGENT_ID), state=WorkerState.STOPPED, report_path=""
    )

    grafted = graft_worker_trajectories(atif_document(), [worker])

    assert [entry["trajectory_id"] for entry in grafted["subagent_trajectories"]] == ["sub-1", WORKER_AGENT_ID]
    assert len(grafted["steps"][1]["observation"]["results"][0]["subagent_trajectory_ref"]) == 1


def test_workspace_trajectory_embeds_workers_and_still_validates() -> None:
    worker = EmbeddedWorker(
        launch=_launch(), document=worker_document(WORKER_AGENT_ID), state=WorkerState.STOPPED, report_path=""
    )

    built = build_workspace_trajectory(
        json.dumps(atif_document_with_worker_launch()), _provenance(), _usage(message_count=2), workers=[worker]
    ).to_json_dict()

    assert [entry["trajectory_id"] for entry in built["subagent_trajectories"]] == ["sub-1", WORKER_AGENT_ID]
    assert (
        built["steps"][2]["observation"]["results"][0]["subagent_trajectory_ref"][0]["trajectory_id"]
        == WORKER_AGENT_ID
    )
    assert built["extra"]["minds_evals"]["source"] == "workspace"


def test_workspace_trajectory_refuses_two_workers_with_one_id() -> None:
    workers = [
        EmbeddedWorker(
            launch=_launch(name=name),
            document=worker_document(WORKER_AGENT_ID),
            state=WorkerState.STOPPED,
            report_path="",
        )
        for name in (WORKER_NAME, "twin")
    ]

    with pytest.raises(TrajectoryDocumentError, match="not valid ATIF"):
        build_workspace_trajectory(
            json.dumps(atif_document_with_worker_launch()), _provenance(), _usage(message_count=2), workers=workers
        )


def test_worker_trajectory_from_stream_is_built_with_mngrs_own_builder() -> None:
    document = build_worker_trajectory_from_stream(worker_stream_jsonl(WORKER_AGENT_ID), WORKER_AGENT_ID, "claude")

    assert (document["session_id"], document["trajectory_id"], document["agent"]["name"]) == (
        WORKER_AGENT_ID,
        WORKER_AGENT_ID,
        "claude",
    )
    assert [(step["source"], step["message"]) for step in document["steps"]] == [
        ("user", "Harden the todo app and report back."),
        ("agent", "Hardened; report pushed."),
    ]
    assert document["final_metrics"]["total_prompt_tokens"] == 700


def test_worker_trajectory_from_a_headerless_stream_is_refused() -> None:
    with pytest.raises(TrajectoryDocumentError, match="cannot be built"):
        build_worker_trajectory_from_stream('{"type": "step", "source": "user", "message": "x"}\n', "w", "claude")


def test_parse_worker_document_accepts_what_the_workspace_built() -> None:
    assert parse_worker_document(json.dumps(worker_document(WORKER_AGENT_ID))) == worker_document(WORKER_AGENT_ID)


@pytest.mark.parametrize(
    ("document", "expected_match"),
    [
        ({**worker_document(WORKER_AGENT_ID), "steps": []}, "not valid ATIF"),
        ({**worker_document(WORKER_AGENT_ID), "trajectory_id": None}, "no trajectory_id"),
        ([1, 2], "not a JSON object"),
    ],
)
def test_parse_worker_document_refuses_what_cannot_be_embedded(document: object, expected_match: str) -> None:
    with pytest.raises(TrajectoryDocumentError, match=expected_match):
        parse_worker_document(json.dumps(document))
