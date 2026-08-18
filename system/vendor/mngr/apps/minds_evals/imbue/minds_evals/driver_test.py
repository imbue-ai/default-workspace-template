import asyncio
import json
from pathlib import Path

import pytest
from harbor.models.agent.context import AgentContext

from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.driver import LiteralTurnSource
from imbue.minds_evals.driver import MindsPersonaDriver
from imbue.minds_evals.driver import PersonaLLMTurnSource
from imbue.minds_evals.driver import SnapshotMode
from imbue.minds_evals.driver import _new_agent_reply_texts
from imbue.minds_evals.driver import derive_user_id
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.driver import parse_snapshot_mode
from imbue.minds_evals.driver import resolve_turn_sources
from imbue.minds_evals.driver import sanitize_user_id
from imbue.minds_evals.errors import InstructionParseError
from imbue.minds_evals.mock_environment_test import ConversationModel
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result


def _case_config(prompts: tuple[str, ...], timeout_seconds: float = 1800.0) -> CaseConfig:
    return CaseConfig(
        case_id="todo-app",
        persona="Non-technical founder.",
        prompts=prompts,
        timeout_seconds=timeout_seconds,
        mngr_branch="main",
        mngr_sha="a" * 40,
        dwt_repo="https://example.invalid/dwt.git",
        dwt_branch="main",
        avg_word_count_baseline=100.0,
    )


def _instruction_for(case_config: CaseConfig) -> str:
    return "# Task\n\nDo the thing.\n\n```json\n{}\n```\n".format(json.dumps(case_config.model_dump(), indent=2))


def test_parse_case_config_round_trips_the_fenced_json_block() -> None:
    case_config = _case_config(("Build it", "Sounds good."))

    assert parse_case_config(_instruction_for(case_config)) == case_config


def test_parse_case_config_rejects_instruction_without_json_block() -> None:
    with pytest.raises(InstructionParseError, match="fenced json"):
        parse_case_config("# Task with no config block")


def test_parse_case_config_rejects_unparseable_json() -> None:
    with pytest.raises(InstructionParseError, match="not valid JSON"):
        parse_case_config("```json\n{nope\n```")


def test_sanitize_user_id_lowercases_and_collapses_dashes() -> None:
    assert sanitize_user_id("Todo App__X9!") == "todo-app-x9"


def test_derive_user_id_appends_salt_and_bounds_length() -> None:
    user_id = derive_user_id("a-very-long-trial-name-that-goes-on-forever__shortid", "cafe1234")

    assert user_id.endswith("-cafe1234")
    assert len(user_id) <= 40


def test_parse_snapshot_mode_accepts_cli_spellings() -> None:
    assert parse_snapshot_mode("per-turn") == SnapshotMode.PER_TURN
    assert parse_snapshot_mode("final") == SnapshotMode.FINAL
    assert parse_snapshot_mode("off") == SnapshotMode.OFF


def test_resolve_turn_sources_maps_literals_and_shares_the_persona_source() -> None:
    case_config = _case_config(("Build it", DECIDE_SENTINEL, "Sounds good.", DECIDE_SENTINEL))

    sources = resolve_turn_sources(case_config, "claude-opus-4-8", "")

    assert isinstance(sources[0], LiteralTurnSource)
    assert sources[0].prompt == "Build it"
    assert isinstance(sources[1], PersonaLLMTurnSource)
    assert isinstance(sources[3], PersonaLLMTurnSource)
    assert sources[1] is sources[3]


def test_new_agent_reply_texts_only_counts_replies_after_the_baseline() -> None:
    events = [
        {"type": "assistant_message", "text": "old reply"},
        {"type": "user_message", "content": "our turn"},
        {"type": "assistant_message", "text": ""},
        {"type": "assistant_message", "text": "new reply"},
        # A framework-injected user message after the reply must not hide it.
        {"type": "user_message", "content": "injected /welcome body"},
    ]

    # Baseline before our turn (index 1): the new reply is found, the old one ignored.
    assert _new_agent_reply_texts(events, 1) == ["new reply"]
    # Baseline past the reply: nothing new.
    assert _new_agent_reply_texts(events, 5) == []


def _write_modal_config(tmp_path: Path) -> Path:
    modal_config = tmp_path / "modal.toml"
    modal_config.write_text('[default]\ntoken_id = "ak-test"\ntoken_secret = "as-test"\nactive = true\n')
    return modal_config


def _setup_rules() -> list[ScriptedExecRule]:
    """The scripted box for everything except the stateful conversation endpoints (which the
    ConversationModel serves): boot, workspace create, clone prep, snapshot, and cleanup."""
    activation_script = (
        "# Activated env 'staging'.\n"
        "export MINDS_ROOT_NAME=minds-staging\n"
        "export MNGR_HOST_DIR=/root/.minds-staging/mngr\n"
        "export MNGR_PREFIX=minds-staging-\n"
        "unset MODAL_PROFILE\n"
    )
    return [
        ScriptedExecRule("cat /work/mngr_sha", [ok_result("b" * 40 + "\n")]),
        ScriptedExecRule("minds env activate", [ok_result(activation_script)]),
        ScriptedExecRule("setsid nohup /usr/local/bin/entrypoint.sh", [ok_result()]),
        ScriptedExecRule("probe_minds_port.py", [ok_result("8123\n")]),
        ScriptedExecRule(
            "-X POST http://127.0.0.1:8123/api/v1/workspaces", [ok_result('{"operation_id": "op-1"}\n202')]
        ),
        ScriptedExecRule("operations/create/op-1", [ok_result('{"is_done": true, "agent_id": "ws-1"}\n200')]),
        ScriptedExecRule("tar czf /tmp/post_message", [ok_result(mngr_exec_json(""))]),
        ScriptedExecRule("mngr rsync", [ok_result()]),
        ScriptedExecRule("mngr list --ids", [ok_result()]),
    ]


def _reply_events(reply_text: str) -> list[dict]:
    """The events the workspace produces when a turn is sent: the echoed user message plus the
    agent's reply (with a leading empty/internal assistant event, as the real stream carries)."""
    return [
        {"type": "user_message", "content": "sent"},
        {"type": "assistant_message", "text": ""},
        {"type": "assistant_message", "text": reply_text},
    ]


def _run_driver(
    tmp_path: Path,
    prompts: tuple[str, ...],
    conversation: ConversationModel,
    trial_name: str,
    timeout_seconds: float,
    rules: list[ScriptedExecRule] | None = None,
) -> tuple[MindsPersonaDriver, MockBoxEnvironment, AgentContext]:
    logs_dir = tmp_path / "jobs" / trial_name / "agent"
    logs_dir.mkdir(parents=True)
    driver = MindsPersonaDriver(
        logs_dir=logs_dir,
        modal_config_path=str(_write_modal_config(tmp_path)),
        poll_seconds=0.01,
    )
    environment = MockBoxEnvironment(
        tmp_path, rules if rules is not None else _setup_rules(), conversation=conversation
    )
    context = AgentContext()
    case_config = _case_config(prompts, timeout_seconds)

    async def _drive() -> None:
        await driver.setup(environment)
        await driver.run(_instruction_for(case_config), environment, context)

    asyncio.run(_drive())
    return driver, environment, context


def test_driver_completes_a_multi_turn_conversation(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[{"type": "user_message", "content": "/welcome"}, {"type": "assistant_message", "text": "Hi!"}],
        turn_reply_events=[
            _reply_events("Building it now; I'll let you know when it's ready."),
            _reply_events("All done. Open the preview to try it out."),
        ],
    )
    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it", "Sounds good."),
        conversation,
        trial_name="todo-app__abc123",
        timeout_seconds=1800.0,
    )

    # The clean conversation carries only the eval's own turns (no /welcome noise).
    conversation_lines = environment.uploaded_content_by_target["/logs/agent/conversation.jsonl"].splitlines()
    parsed = [json.loads(line) for line in conversation_lines]
    assert [(event.get("type"), event.get("content") or event.get("text")) for event in parsed] == [
        ("user_message", "Build it"),
        ("assistant_message", "Building it now; I'll let you know when it's ready."),
        ("user_message", "Sounds good."),
        ("assistant_message", "All done. Open the preview to try it out."),
    ]

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "finished"
    assert state["waits_done"] == 2
    assert state["num_turns"] == 2

    # The per-trial box env carried the Modal token pair, the salted user id, and the key manifest.
    backend_env = next(env for env in environment.exec_envs if env and "MINDS_BOX_MNGR_REF" in env)
    assert backend_env["MODAL_TOKEN_ID"] == "ak-test"
    assert backend_env["MNGR__PROVIDERS__MODAL__USER_ID"].startswith("todo-app-abc123-")
    assert backend_env["MINDS_BOX_MNGR_REF"] == "b" * 40

    # Per-turn snapshots ran and cleanup destroyed the trial's workspaces.
    assert any("post_message_2.tar.gz" in command for command in environment.exec_commands)
    assert any("mngr list --ids | uv run mngr destroy - --force" in command for command in environment.exec_commands)

    assert context.metadata is not None
    assert context.metadata["test_state"] == "finished"
    assert context.metadata["turns_completed"] == 2
    assert context.metadata["average_words_per_turn"] > 0
    assert context.metadata["average_words_per_message"] > 0

    # The ATIF trajectory renders the clean conversation for harbor view.
    trajectory = json.loads((driver.logs_dir / "trajectory.json").read_text())
    assert [step["source"] for step in trajectory["steps"]] == ["user", "agent", "user", "agent"]


def test_driver_marks_timed_out_when_no_reply_arrives(tmp_path: Path) -> None:
    # The agent echoes the user message but never produces a reply, so the tiny budget expires.
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[[{"type": "user_message", "content": "sent"}]],
    )
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__timeout1",
        timeout_seconds=0.3,
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert state["timed_out"] is True
    assert context.metadata is not None
    assert context.metadata["timed_out"] is True
    # Cleanup still ran.
    assert any("mngr destroy - --force" in command for command in environment.exec_commands)
