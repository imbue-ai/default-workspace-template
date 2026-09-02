"""Unit tests for the grade-time judge-transcript renderer. The renderer ships as a self-contained
verifier-container script under templates/tests/ (stdlib only, not a package module), so it is loaded
by file path rather than imported as ``imbue.minds_evals.templates...``."""

import importlib.util
import json
from pathlib import Path
from typing import Any

_RENDERER_PATH = Path(__file__).parent / "templates" / "tests" / "render_judge_transcript.py"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("minds_evals_judge_renderer", _RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RENDERER = _load_renderer()


def _sample_events() -> list[dict[str, Any]]:
    """A synthetic full_transcript covering every event kind: the /welcome trigger, two is_meta
    skill-body ingestions, real client turns, non-empty agent messages, empty/tool-only assistant
    events, a tool_result, and the driver's appended decider_message."""
    return [
        {"type": "user_message", "content": "/welcome"},
        {"type": "user_message", "content": "WELCOME SKILL BODY -- 1738 chars of instructions", "is_meta": True},
        {"type": "user_message", "content": "hi what can you do"},
        {"type": "assistant_message", "text": ""},
        {"type": "assistant_message", "text": "Hey! I can build you small web apps you open as a tab."},
        {
            "type": "assistant_message",
            "text": "",
            "tool_calls": [{"tool_name": "Skill", "input_preview": '{"skill":"build-app"}'}],
        },
        {"type": "tool_result", "tool_name": "Skill", "output": "Launching skill: build-app"},
        {"type": "user_message", "content": "BUILD-APP SKILL BODY -- 24585 chars of instructions", "is_meta": True},
        {"type": "assistant_message", "text": "Here's my plan: a simple task tracker, just for you."},
        {"type": "user_message", "content": "Looks good, go for it."},
        {"type": "assistant_message", "text": "Building it now."},
        {"type": "decider_message", "turn": 2, "text": "SIMULATED CLIENT MESSAGE", "is_fallback": False},
    ]


def test_render_keeps_only_client_turns_and_numbered_agent_messages() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_events())

    blocks = rendered.split("\n\n")
    headers = [block.splitlines()[0] for block in blocks]
    # Two real client turns and three non-empty agent messages, numbered running
    # across the whole conversation (message 2 lands before the second client turn).
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


def test_render_omits_framework_noise_tools_and_decider_events() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_events())

    # No /welcome trigger, no is_meta skill bodies, no tool call/result, no empty
    # agent event surfacing as a block, and no appended decider audit message.
    assert "/welcome" not in rendered
    assert "SKILL BODY" not in rendered
    assert "Launching skill" not in rendered
    assert "build-app" not in rendered
    assert "SIMULATED CLIENT MESSAGE" not in rendered
    assert "tool_calls" not in rendered
    # Exactly three agent blocks: the empty and tool-only assistant events were dropped.
    assert "[AGENT · message 4]" not in rendered
    assert rendered.count("[AGENT · message") == 3


def _atif_sample_events() -> list[dict[str, Any]]:
    """The same conversation as ``_sample_events``, in the ATIF-shaped records mngr emits."""
    return [
        {"type": "header", "event_id": "header", "emitter": "claude/common_transcript"},
        {"type": "step", "source": "user", "message": "/welcome"},
        # Framework-injected text is a system step in this vintage, not an is_meta user message.
        {"type": "step", "source": "system", "message": "WELCOME SKILL BODY -- 1738 chars"},
        {"type": "step", "source": "user", "message": "hi what can you do"},
        {"type": "step", "source": "agent", "message": ""},
        {"type": "step", "source": "agent", "message": "Hey! I can build you small web apps you open as a tab."},
        {
            "type": "step",
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": "c1", "function_name": "Skill", "arguments": {"skill": "build-app"}}],
        },
        {
            "type": "observation",
            "results": [{"source_call_id": "c1", "content": "Launching skill: build-app"}],
        },
        {"type": "step", "source": "system", "message": "BUILD-APP SKILL BODY -- 24585 chars"},
        {"type": "step", "source": "agent", "message": "Here's my plan: a simple task tracker, just for you."},
        {"type": "step", "source": "user", "message": "Looks good, go for it."},
        {"type": "step", "source": "agent", "message": "Building it now."},
        {"type": "decider_message", "turn": 2, "text": "SIMULATED CLIENT MESSAGE", "is_fallback": False},
    ]


def test_render_of_atif_records_matches_the_legacy_rendering() -> None:
    assert _RENDERER.render_judge_transcript(_atif_sample_events()) == _RENDERER.render_judge_transcript(
        _sample_events()
    )


def test_render_of_atif_records_omits_system_steps_observations_and_the_header() -> None:
    rendered = _RENDERER.render_judge_transcript(_atif_sample_events())

    assert "/welcome" not in rendered
    assert "SKILL BODY" not in rendered
    assert "Launching skill" not in rendered
    assert "SIMULATED CLIENT MESSAGE" not in rendered
    assert "common_transcript" not in rendered
    assert rendered.count("[AGENT · message") == 3


def test_render_of_empty_stream_is_empty() -> None:
    assert _RENDERER.render_judge_transcript([]) == ""


def test_load_transcript_parses_jsonl_and_tolerates_bad_lines(tmp_path: Path) -> None:
    transcript_path = tmp_path / "full_transcript.jsonl"
    lines = [json.dumps(event) for event in _sample_events()]
    # Blank and unparseable lines must be tolerated (skipped), not abort the render.
    transcript_path.write_text("\n".join([lines[0], "", "{not json", *lines[1:]]) + "\n")

    events = _RENDERER._load_transcript(transcript_path)

    assert len(events) == len(lines)
    rendered = _RENDERER.render_judge_transcript(events)
    assert "[USER]\nhi what can you do" in rendered
    assert rendered.count("[AGENT · message") == 3


def test_load_transcript_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert _RENDERER._load_transcript(tmp_path / "does-not-exist.jsonl") == []
