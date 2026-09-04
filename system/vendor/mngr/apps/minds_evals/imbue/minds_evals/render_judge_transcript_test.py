"""Unit tests for the grade-time judge-transcript renderer. The renderer ships as a self-contained
verifier-container script under templates/tests/verifier/ (stdlib only, not a package module), so it is loaded
by file path rather than imported as ``imbue.minds_evals.templates...``."""

import json
from pathlib import Path
from typing import Any

from imbue.minds_evals.data_types import StepBoundary
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import atif_document
from imbue.minds_evals.trajectory import build_hand_built_trajectory
from imbue.minds_evals.usage import summarize_workspace_usage

_RENDERER = load_template_module("tests/verifier/render_judge_transcript.py", "minds_evals_judge_renderer")


def _sample_steps() -> list[dict[str, Any]]:
    """A workspace-shaped trajectory covering every step kind: framework-injected system steps, real
    client turns, non-empty agent messages, and tool-only agent inferences."""
    return [
        {"step_id": 1, "source": "system", "message": "WELCOME SKILL BODY -- 1738 chars of instructions"},
        {"step_id": 2, "source": "user", "message": "hi what can you do"},
        {"step_id": 3, "source": "agent", "message": ""},
        {"step_id": 4, "source": "agent", "message": "Hey! I can build you small web apps you open as a tab."},
        {
            "step_id": 5,
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": "c1", "function_name": "Skill", "arguments": {"skill": "build-app"}}],
            "observation": {"results": [{"source_call_id": "c1", "content": "Launching skill: build-app"}]},
        },
        {"step_id": 6, "source": "system", "message": "BUILD-APP SKILL BODY -- 24585 chars of instructions"},
        {"step_id": 7, "source": "agent", "message": "Here's my plan: a simple task tracker, just for you."},
        {"step_id": 8, "source": "user", "message": "Looks good, go for it."},
        {"step_id": 9, "source": "agent", "message": "Building it now."},
    ]


def test_render_keeps_only_client_turns_and_numbered_agent_messages() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_steps())

    blocks = rendered.split("\n\n")
    headers = [block.splitlines()[0] for block in blocks]
    # Two client turns and three non-empty agent messages, numbered running across the whole
    # conversation (message 2 lands before the second client turn).
    assert headers == [
        "[USER]",
        "[AGENT · message 1]",
        "[AGENT · message 2]",
        "[USER]",
        "[AGENT · message 3]",
    ]
    assert "hi what can you do" in rendered
    assert "Looks good, go for it." in rendered
    assert "Building it now." in rendered


def test_render_omits_system_steps_and_tool_only_inferences() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_steps())

    # Framework-injected text and tool plumbing carry nothing the client would see.
    assert "SKILL BODY" not in rendered
    assert "Launching skill" not in rendered
    assert "build-app" not in rendered
    assert "tool_calls" not in rendered
    assert rendered.count("[AGENT · message") == 3


def test_render_of_the_hand_built_shape_is_one_block_per_turn() -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {"step_id": 2, "source": "agent", "message": "Building it now.\n\nAll done."},
    ]

    assert (
        _RENDERER.render_judge_transcript(steps)
        == "[USER]\nBuild it\n\n[AGENT · message 1]\nBuilding it now.\n\nAll done.\n"
    )


def test_render_of_no_steps_is_empty() -> None:
    assert _RENDERER.render_judge_transcript([]) == ""


def test_load_trajectory_steps_reads_the_documents_steps(tmp_path: Path) -> None:
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps(atif_document()))

    steps = _RENDERER.load_trajectory_steps(trajectory_path)

    assert [step["source"] for step in steps] == ["user", "agent"]
    assert "[AGENT · message 1]\nBuilding it now." in _RENDERER.render_judge_transcript(steps)


def test_load_trajectory_steps_of_a_missing_or_malformed_file_is_empty(tmp_path: Path) -> None:
    assert _RENDERER.load_trajectory_steps(tmp_path / "does-not-exist.json") == []
    (tmp_path / "not-json.json").write_text("{not json")
    assert _RENDERER.load_trajectory_steps(tmp_path / "not-json.json") == []
    (tmp_path / "not-a-document.json").write_text("[1, 2]")
    assert _RENDERER.load_trajectory_steps(tmp_path / "not-a-document.json") == []


def test_a_harness_step_boundary_never_reaches_the_judge() -> None:
    """The boundary markers the driver writes into a stepped task's trajectory are cosmetic, and the
    renderer's system-step rule is what keeps them out of what a judge scores."""
    built = build_hand_built_trajectory(
        [{"role": "user", "text": "Now change it"}, {"role": "agent", "text": "Changed."}],
        TrajectoryProvenance(
            driver_name="minds-persona-driver",
            driver_version="0.1.0",
            decider_model="claude-opus-4-8",
            decider_turns=(),
            harbor_session_id="session-1",
            case_id="todo-app",
            usage_source=UsageSource.TRANSCRIPT,
        ),
        summarize_workspace_usage(()),
        timestamp="2026-09-01T00:00:00Z",
        boundaries=(
            StepBoundary(
                name="adjust-requirements",
                started_at="2026-09-01T00:00:00Z",
                conversation_index=0,
                opening_message="Now change it",
            ),
        ),
    )

    assert built is not None
    steps = built.to_json_dict()["steps"]
    assert steps[0]["source"] == "system"

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "adjust-requirements" not in rendered
    assert rendered == "[USER]\nNow change it\n\n[AGENT \u00b7 message 1]\nChanged.\n"
