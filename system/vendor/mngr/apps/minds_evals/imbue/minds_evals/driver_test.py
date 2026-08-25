import asyncio
import json
import shlex
import subprocess
from pathlib import Path

import pytest
from harbor.models.agent.context import AgentContext
from pydantic import ValidationError

from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.driver import LiteralTurnSource
from imbue.minds_evals.driver import MindsPersonaDriver
from imbue.minds_evals.driver import PersonaLLMTurnSource
from imbue.minds_evals.driver import SnapshotMode
from imbue.minds_evals.driver import _new_agent_reply_texts
from imbue.minds_evals.driver import build_eval_base_clone_command
from imbue.minds_evals.driver import build_eval_case_commit_command
from imbue.minds_evals.driver import derive_user_id
from imbue.minds_evals.driver import parse_agent_flag
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.driver import parse_snapshot_mode
from imbue.minds_evals.driver import resolve_turn_sources
from imbue.minds_evals.driver import sanitize_user_id
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import InstructionParseError
from imbue.minds_evals.expectations import lower_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.mock_environment_test import ConversationModel
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result
from imbue.minds_evals.testing import commit_readme_revision
from imbue.minds_evals.testing import make_local_git_repo


def _case_config(
    prompts: tuple[str, ...],
    timeout_seconds: float = 1800.0,
    expectations: Expectations | None = None,
) -> CaseConfig:
    return CaseConfig(
        case_id="todo-app",
        persona="Non-technical founder.",
        prompts=prompts,
        timeout_seconds=timeout_seconds,
        verification_timeout_seconds=600.0,
        mngr_branch="main",
        mngr_sha="a" * 40,
        dwt_repo="https://example.invalid/dwt.git",
        dwt_branch="main",
        dwt_sha="c" * 40,
        avg_word_count_baseline=100.0,
        expectations=lower_expectations(expectations) if expectations is not None else None,
        authored_expectations=expectations,
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


def test_parse_case_config_rejects_a_case_without_a_pinned_workspace_template() -> None:
    """A dataset generated before the template was pinned must fail the trial rather than quietly
    build its workspaces from whatever the branch points at now."""
    unpinned_case = _case_config(("Build it",)).model_dump()
    del unpinned_case["dwt_sha"]
    instruction = "# Task\n\n```json\n{}\n```\n".format(json.dumps(unpinned_case, indent=2))

    with pytest.raises(ValidationError, match="dwt_sha"):
        parse_case_config(instruction)


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


def _git_output(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_build_eval_base_clone_command_lands_the_pinned_sha_on_a_real_branch(tmp_path: Path) -> None:
    # Pinned to the middle commit: a command that resolved the branch instead of
    # the sha would land on the tip.
    source = make_local_git_repo(tmp_path, "fake-dwt", commit_count=3)
    pinned_sha = source.commit_shas[1]
    eval_base_dir = tmp_path / "eval-base"

    command = build_eval_base_clone_command(
        dwt_repo=str(source.repo_dir),
        dwt_branch="main",
        dwt_sha=pinned_sha,
        eval_base_dir=str(eval_base_dir),
    )
    subprocess.run(["bash", "-c", command], check=True, capture_output=True)

    assert _git_output(eval_base_dir, "rev-parse", "HEAD") == pinned_sha
    # A named branch, not a detached HEAD: every downstream clone takes its
    # checkout from this HEAD, and the workspace is created with an empty branch
    # field, meaning "whatever HEAD is".
    assert _git_output(eval_base_dir, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    # The per-case clone, then mngr's clone of that, must come out populated at the pin.
    case_clone_dir = tmp_path / "case-clone"
    subprocess.run(["git", "clone", "-q", str(eval_base_dir), str(case_clone_dir)], check=True)
    workspace_clone_dir = tmp_path / "workspace-clone"
    subprocess.run(["git", "clone", "-q", str(case_clone_dir), str(workspace_clone_dir)], check=True)

    assert (workspace_clone_dir / "README.md").read_text() == "fake-dwt revision 1\n"
    assert _git_output(workspace_clone_dir, "rev-parse", "HEAD") == pinned_sha
    assert _git_output(workspace_clone_dir, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_build_eval_base_clone_command_pins_a_sha_off_a_non_default_branch(tmp_path: Path) -> None:
    """A dwt branch other than the remote's default must still be reachable in the box clone."""
    source = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    subprocess.run(["git", "-C", str(source.repo_dir), "checkout", "-q", "-b", "codex/harness"], check=True)
    pinned_sha = commit_readme_revision(source.repo_dir, "side branch\n", "side")
    subprocess.run(["git", "-C", str(source.repo_dir), "checkout", "-q", "main"], check=True)
    eval_base_dir = tmp_path / "eval-base"

    command = build_eval_base_clone_command(
        dwt_repo=str(source.repo_dir),
        dwt_branch="codex/harness",
        dwt_sha=pinned_sha,
        eval_base_dir=str(eval_base_dir),
    )
    subprocess.run(["bash", "-c", command], check=True, capture_output=True)

    assert _git_output(eval_base_dir, "rev-parse", "HEAD") == pinned_sha
    assert _git_output(eval_base_dir, "rev-parse", "--abbrev-ref", "HEAD") == "codex/harness"
    assert (eval_base_dir / "README.md").read_text() == "side branch\n"


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
    workspace_state = (
        "<<<MINDS_EVALS_SECTION:repo_root>>>\n/home/user/workspace\n"
        "<<<MINDS_EVALS_SECTION:registry_status>>>\npresent\n"
        "<<<MINDS_EVALS_SECTION:registry>>>\n"
        '[[apps]]\nname = "todo"\nurl = "http://localhost:8081"\nlabel = "todo-bb"\n'
        "<<<MINDS_EVALS_SECTION:services>>>\ntodo   RUNNING   pid 103\n"
        "<<<MINDS_EVALS_SECTION:supervisord>>>\n"
        "[program:todo]\ncommand=python3 system/scripts/forward_port.py "
        "--url http://localhost:8081 --name todo\n"
        "<<<MINDS_EVALS_SECTION:isolated_instances>>>\n"
    )
    return [
        ScriptedExecRule("cat /work/mngr_sha", [ok_result("b" * 40 + "\n")]),
        ScriptedExecRule("minds-admin env activate", [ok_result(activation_script)]),
        ScriptedExecRule("setsid nohup /usr/local/bin/entrypoint.sh", [ok_result()]),
        ScriptedExecRule("probe_minds_port.py", [ok_result("8123\n")]),
        ScriptedExecRule(
            "-X POST http://127.0.0.1:8123/api/v1/workspaces", [ok_result('{"operation_id": "op-1"}\n202')]
        ),
        ScriptedExecRule("operations/create/op-1", [ok_result('{"is_done": true, "agent_id": "ws-1"}\n200')]),
        ScriptedExecRule(
            "MINDS_EVALS_SECTION:base_sha",
            [
                ok_result(
                    "<<<MINDS_EVALS_SECTION:base_sha>>>\n{}\n<<<MINDS_EVALS_SECTION:dwt_tip_sha>>>\n{}\n".format(
                        "c" * 40, "e" * 40
                    )
                )
            ],
        ),
        ScriptedExecRule("tar czf /tmp/post_message", [ok_result(mngr_exec_json(""))]),
        # The evidence-collection phase, which runs against the live workspace before teardown.
        ScriptedExecRule("MINDS_EVALS_SECTION:repo_root", [ok_result(mngr_exec_json(workspace_state))]),
        ScriptedExecRule("base64 -d | python3 -", [ok_result(mngr_exec_json("7\n"))]),
        ScriptedExecRule(
            "git bundle create",
            [
                ok_result(
                    mngr_exec_json(
                        "<<<MINDS_EVALS_SECTION:head_sha>>>\n{}\n"
                        "<<<MINDS_EVALS_SECTION:status>>>\n"
                        "<<<MINDS_EVALS_SECTION:commit_count>>>\n2\n"
                        "<<<MINDS_EVALS_SECTION:bundle>>>\n".format("d" * 40)
                    )
                )
            ],
        ),
        ScriptedExecRule(
            "http_headers",
            [
                ok_result(
                    mngr_exec_json(
                        "<<<MINDS_EVALS_SECTION:status>>>\n200 0.01\n"
                        "<<<MINDS_EVALS_SECTION:headers>>>\nHTTP/1.1 200 OK\n"
                        "<<<MINDS_EVALS_SECTION:body>>>\n<h1>todo</h1>"
                    )
                )
            ],
        ),
        ScriptedExecRule("mngr rsync", [ok_result()]),
        ScriptedExecRule("mngr list --ids", [ok_result()]),
    ]


def _reply_events(reply_text: str, usage: dict | None = None) -> list[dict]:
    """The events the workspace produces when a turn is sent: the echoed user message plus the
    agent's reply (with a leading empty/internal assistant event, as the real stream carries).
    ``usage`` mirrors the per-message accounting the real transcript attaches to agent messages."""
    reply: dict = {"type": "assistant_message", "text": reply_text}
    if usage is not None:
        reply = {**reply, "model": "claude-opus-4-8", "usage": usage}
    return [
        {"type": "user_message", "content": "sent"},
        {"type": "assistant_message", "text": ""},
        reply,
    ]


def _run_driver(
    tmp_path: Path,
    prompts: tuple[str, ...],
    conversation: ConversationModel,
    trial_name: str,
    timeout_seconds: float,
    rules: list[ScriptedExecRule] | None = None,
    expectations: Expectations | None = None,
) -> tuple[MindsPersonaDriver, MockBoxEnvironment, AgentContext]:
    logs_dir = tmp_path / "jobs" / trial_name / "agent"
    logs_dir.mkdir(parents=True)
    driver = MindsPersonaDriver(
        logs_dir=logs_dir,
        modal_config_path=str(_write_modal_config(tmp_path)),
        poll_seconds=0.01,
        # The key the driver signs the workspace in with, supplied the way harbor supplies it.
        extra_env={"ANTHROPIC_API_KEY": "sk-eval-test"},
    )
    environment = MockBoxEnvironment(
        tmp_path, rules if rules is not None else _setup_rules(), conversation=conversation
    )
    context = AgentContext()
    case_config = _case_config(prompts, timeout_seconds, expectations)

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
    # Both pinned inputs travel with the trial record.
    assert state["mngr_sha"] == "b" * 40
    assert state["dwt_sha"] == "c" * 40

    # The workspace template clone is driven from the case's pinned sha.
    assert any("checkout -B main {}".format("c" * 40) in command for command in environment.exec_commands)

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
    assert context.metadata["dwt_sha"] == "c" * 40
    assert context.metadata["average_words_per_turn"] > 0
    assert context.metadata["average_words_per_message"] > 0

    # The ATIF trajectory renders the clean conversation for harbor view.
    trajectory = json.loads((driver.logs_dir / "trajectory.json").read_text())
    assert [step["source"] for step in trajectory["steps"]] == ["user", "agent", "user", "agent"]


def test_driver_signs_the_workspace_in_before_the_first_turn(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("Building it now.")],
    )
    _driver, environment, _context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__auth1",
        timeout_seconds=1800.0,
    )

    # Signed in through the product's own endpoint, carrying the key as a credential paste.
    assert len(conversation.submitted_credential_commands) == 1
    assert "ANTHROPIC_API_KEY=sk-eval-test" in conversation.submitted_credential_commands[0]
    # ...and never through the create-time host env, which is the regime production does not use.
    backend_env = next(env for env in environment.exec_envs if env and "MINDS_BOX_MNGR_REF" in env)
    assert "ANTHROPIC_API_KEY" not in backend_env
    assert "MINDS_EXTRA_PASS_HOST_ENV" not in backend_env
    assert "MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR" not in backend_env


def test_driver_marks_timed_out_when_the_workspace_cannot_be_signed_in(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("Building it now.")],
    )
    conversation.is_auth_endpoint_up = False
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__auth2",
        timeout_seconds=0.3,
    )

    # An unauthenticated workspace can never take a turn, so the trial fails at the gate rather
    # than burning its budget and being graded on refusal text.
    assert conversation.submitted_credential_commands == []
    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 0


def test_driver_reports_the_workspace_agents_usage_and_keeps_the_decider_separate(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[
            _reply_events(
                "Building it now.",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_tokens": 5_000,
                    "cache_write_tokens": 2_000,
                },
            ),
            _reply_events(
                "All done.",
                usage={
                    "input_tokens": 5,
                    "output_tokens": 50,
                    "cache_read_tokens": 6_000,
                    "cache_write_tokens": 0,
                },
            ),
        ],
    )
    driver, _environment, context = _run_driver(
        tmp_path,
        ("Build it", "Sounds good."),
        conversation,
        trial_name="todo-app__usage1",
        timeout_seconds=1800.0,
    )

    # Harbor's fields carry the workspace agent, cache-inclusive on input.
    assert context.n_input_tokens == 15 + 11_000 + 2_000
    assert context.n_cache_tokens == 11_000
    assert context.n_output_tokens == 150
    assert context.cost_usd is not None and context.cost_usd > 0

    # Both turns are literal, so the decider never ran -- and its (empty) accounting is metadata,
    # never folded into the agent's own numbers.
    assert context.metadata is not None
    assert context.metadata["decider_usage"]["call_count"] == 0
    workspace_usage = context.metadata["workspace_usage"]
    assert workspace_usage["tokens"] == {"input": 15, "output": 150, "cache_read": 11_000, "cache_write": 2_000}
    assert workspace_usage["per_model"][0]["model"] == "claude-opus-4-8"

    # The breakdown is also its own artifact, and the trajectory carries the same totals.
    usage_artifact = json.loads((driver.logs_dir / "usage.json").read_text())
    assert usage_artifact["workspace_agent"]["cost_usd"] == context.cost_usd
    trajectory = json.loads((driver.logs_dir / "trajectory.json").read_text())
    assert trajectory["final_metrics"]["total_cached_tokens"] == 11_000
    assert trajectory["final_metrics"]["total_cost_usd"] == context.cost_usd


def test_driver_leaves_usage_unset_when_the_transcript_carries_none(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("Building it now.")],
    )
    _driver, _environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__nousage1",
        timeout_seconds=1800.0,
    )

    # An unknown cost must stay unknown rather than being reported as zero.
    assert context.n_input_tokens is None
    assert context.cost_usd is None
    assert context.metadata is not None
    assert context.metadata["workspace_usage"]["cost_usd"] is None


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


def test_driver_fails_when_the_workspace_reports_the_wrong_auth_mode(tmp_path: Path) -> None:
    # The sign-in endpoint runs no credential probe, so it accepts a bad key and reports the mode it
    # ended up in. Without checking that, the trial would run on an unauthenticated workspace and
    # the judge would grade the agent's "not logged in" replies as if they were its own behaviour.
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("Building it now.")],
    )
    conversation.expected_auth_mode = "none"
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__auth3",
        timeout_seconds=1800.0,
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 0


def test_driver_collects_outcome_evidence_before_the_workspace_is_torn_down(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("All done; open the preview.")],
    )
    expectations = parse_expectations(
        {"outcome": "A working to-do web app.", "deliverable": {"kind": "minds-app"}}, "todo-app"
    )

    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__evidence1",
        timeout_seconds=1800.0,
        expectations=expectations,
    )

    manifest = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/manifest.json"])
    statuses = {entry["entry_id"]: entry["status"] for entry in manifest["entries"]}
    assert statuses["app_registered"] == "passed"
    assert statuses["app_registered_service_todo"] == "passed"
    assert statuses["http_0_registered_apps_todo"] == "passed"
    assert manifest["is_evidence_complete"] is True

    # The bundle's base is the prepared clone's HEAD, so only the agent's own commits are captured.
    repo_state = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/repo_state.json"])
    assert repo_state["base_sha"] == "c" * 40
    assert repo_state["head_sha"] == "d" * 40
    # The dwt tip travels too, so a replay can regenerate the base and check it reproduces base_sha.
    assert repo_state["dwt_tip_sha"] == "e" * 40
    assert manifest["base_sha"] == "c" * 40
    assert manifest["dwt_tip_sha"] == "e" * 40

    # Collection happens while the workspace is alive: before the destroy sweep, never after.
    collection_index = next(
        index for index, command in enumerate(environment.exec_commands) if "MINDS_EVALS_SECTION:repo_root" in command
    )
    destroy_index = next(
        index for index, command in enumerate(environment.exec_commands) if "mngr destroy - --force" in command
    )
    assert collection_index < destroy_index

    assert context.metadata is not None
    assert context.metadata["verification"]["is_evidence_complete"] is True


def test_driver_skips_the_expectation_probes_on_a_timed_out_trial(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[[{"type": "user_message", "content": "sent"}]],
    )
    expectations = parse_expectations(
        {"outcome": "A working to-do web app.", "deliverable": {"kind": "minds-app"}}, "todo-app"
    )

    _driver, environment, _context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__evidence2",
        timeout_seconds=0.3,
        expectations=expectations,
    )

    # The gates already zero a timed-out trial, so only the cheap always-on capture runs.
    manifest = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/manifest.json"])
    assert {entry["entry_id"] for entry in manifest["entries"]} == {"file_inventory"}
    assert environment.uploaded_content_by_target["/logs/agent/verification/apps.toml"].strip().startswith("[[apps]]")


def test_driver_collects_workspace_state_even_without_expectations(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("Here is what I can do.")],
    )

    _driver, environment, _context = _run_driver(
        tmp_path,
        ("hi what can you do",),
        conversation,
        trial_name="greeting__evidence3",
        timeout_seconds=1800.0,
    )

    manifest = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/manifest.json"])
    assert manifest["is_expectations_declared"] is False
    assert "/logs/agent/verification/services.txt" in environment.uploaded_content_by_target


def test_driver_creates_the_evidence_directory_even_when_collection_never_runs(tmp_path: Path) -> None:
    # harbor records a missing declared artifact path as a failed entry and refuses to regrade any
    # trial carrying one, while an EMPTY directory is fine. A trial that dies before ever creating
    # a workspace must therefore still leave the directory behind, or it can never be regraded.
    logs_dir = tmp_path / "jobs" / "todo-app__nows1" / "agent"
    logs_dir.mkdir(parents=True)
    driver = MindsPersonaDriver(
        logs_dir=logs_dir,
        modal_config_path=str(_write_modal_config(tmp_path)),
        poll_seconds=0.01,
        extra_env={"ANTHROPIC_API_KEY": "sk-eval-test"},
    )
    environment = MockBoxEnvironment(tmp_path, _setup_rules())

    # setup() alone -- no conversation, no workspace, so the collection phase never runs.
    asyncio.run(driver.setup(environment))

    assert "/logs/agent/verification/manifest.json" not in environment.uploaded_content_by_target
    assert any(command.startswith("mkdir -p /logs/agent/verification") for command in environment.exec_commands)


def test_eval_case_commit_is_reproducible(tmp_path: Path) -> None:
    # A commit hash covers its dates, so a wall-clock commit would give every trial a different base
    # sha for an identical tree -- and the deliverable bundle, based on that commit, could never be
    # unbundled onto a regenerated clone. That replayability is the whole point of capturing it.
    command = build_eval_case_commit_command("'/work/clones/todo-app'", "'eval case todo-app'")

    assert "GIT_AUTHOR_DATE='1970-01-01T00:00:00 +0000'" in command
    assert "GIT_COMMITTER_DATE='1970-01-01T00:00:00 +0000'" in command
    assert "user.email=eval@minds" in command
    assert "user.name=minds-eval" in command

    # And it really is reproducible: the same tree committed twice yields the same sha.
    shas: list[str] = []
    for attempt in ("first", "second"):
        repo = tmp_path / attempt
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "app.py").write_text("print('hi')\n")
        subprocess.run(
            ["bash", "-c", build_eval_case_commit_command(shlex.quote(str(repo)), "'eval case todo-app'")],
            check=True,
        )
        shas.append(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )

    assert shas[0] == shas[1]


def test_parse_agent_flag_accepts_every_form_harbor_can_deliver() -> None:
    # Harbor JSON-parses agent kwargs, so the Python type depends on the spelling: `flag=true`
    # arrives as a bool, `flag=1` as an int, and only `flag=yes` stays a string. All must work, or
    # the trial dies in __init__ before writing any log.
    assert parse_agent_flag(True, "flag") is True
    assert parse_agent_flag(False, "flag") is False
    assert parse_agent_flag("true", "flag") is True
    assert parse_agent_flag("yes", "flag") is True
    assert parse_agent_flag("on", "flag") is True
    assert parse_agent_flag("false", "flag") is False
    assert parse_agent_flag("no", "flag") is False
    assert parse_agent_flag("off", "flag") is False
    # The int and None forms are what `--ak flag=1` and `--ak flag=null` actually deliver. Reading
    # them as strings is what used to raise AttributeError from inside the parser.
    assert parse_agent_flag(1, "flag") is True
    assert parse_agent_flag(0, "flag") is False


def test_parse_agent_flag_rejects_a_value_it_cannot_read() -> None:
    # A flag that silently means "off" whenever it cannot be understood turns a typo into a trial
    # that ran one arm and reported the other. An empty value is included deliberately: `--ak
    # flag=` and `--ak flag=null` are mistakes, not a way to spell False.
    for raw_value in ("maybe", "", None, 2):
        with pytest.raises(AgentKwargError, match="flag"):
            parse_agent_flag(raw_value, "flag")


def test_parse_snapshot_mode_rejects_a_cadence_it_cannot_honour() -> None:
    # `--ak snapshot_mode=1` hands over an int, which used to raise AttributeError from inside the
    # parser; a bad name used to surface as a bare ValueError naming the enum, which reads as an
    # internal fault rather than a bad kwarg.
    for raw_value in ("per turn", 1, None, "sometimes"):
        with pytest.raises(AgentKwargError, match="snapshot_mode"):
            parse_snapshot_mode(raw_value)


def test_driver_refuses_an_unreadable_kwarg_before_anything_boots(tmp_path: Path) -> None:
    # Rejection belongs in __init__: a run that cannot honour its own arguments should stop before
    # it spends a box, not after a trial has been graded under a setting that never applied.
    logs_dir = tmp_path / "logs"
    with pytest.raises(AgentKwargError):
        MindsPersonaDriver(logs_dir=logs_dir, proxy="maybe")
    with pytest.raises(AgentKwargError):
        MindsPersonaDriver(logs_dir=logs_dir, snapshot_mode="per turn")
    with pytest.raises(AgentKwargError):
        MindsPersonaDriver(logs_dir=logs_dir, snapshot_mode=1)


def test_driver_captures_work_the_agent_does_after_it_reports_waiting(tmp_path: Path) -> None:
    # The agent can keep spending after it says WAITING (the workspace's turn-end flow runs then).
    # Those messages exist only in the workspace, so a driver that stops reading at the reply loses
    # them permanently once the workspace is destroyed -- and the trial then reports a cost with no
    # messages behind part of it.
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[
            _reply_events(
                "All done.",
                usage={"input_tokens": 10, "output_tokens": 100, "cache_read_tokens": 0, "cache_write_tokens": 0},
            )
        ],
        trailing_events=[
            {
                "type": "assistant_message",
                "text": "tidying up",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 70,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            }
        ],
    )

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__trailing",
        timeout_seconds=1800.0,
    )

    transcript = environment.uploaded_content_by_target["/logs/agent/full_transcript.jsonl"]
    assert "tidying up" in transcript
    # And its tokens are accounted for, rather than being spend with no record.
    assert context.n_input_tokens == 17
    assert context.n_output_tokens == 170


def test_driver_runs_the_verification_agent_on_the_decider_model_by_default(tmp_path: Path) -> None:
    # Flow driving is mechanical, so a cheaper tier may do -- but until that is measured the
    # verification agent runs on whatever model the decider does.
    driver = MindsPersonaDriver(logs_dir=tmp_path / "agent", extra_env={"ANTHROPIC_API_KEY": "sk-eval-test"})

    agent = driver._build_verification_agent()

    assert isinstance(agent, ui_flows.AnthropicVerificationAgent)
    assert agent.model == driver._decider_model


def test_driver_honours_the_verifier_model_override(tmp_path: Path) -> None:
    driver = MindsPersonaDriver(
        logs_dir=tmp_path / "agent",
        extra_env={"ANTHROPIC_API_KEY": "sk-eval-test"},
        verifier_model="claude-haiku-4-5",
    )

    agent = driver._build_verification_agent()

    assert isinstance(agent, ui_flows.AnthropicVerificationAgent)
    assert agent.model == "claude-haiku-4-5"
    # The override must not drag the simulated client onto a different model -- that would change
    # the thing being measured, not just how it is measured.
    assert driver._decider_model != "claude-haiku-4-5"


def test_driver_builds_no_verification_agent_without_a_key(tmp_path: Path) -> None:
    driver = MindsPersonaDriver(logs_dir=tmp_path / "agent", extra_env={})

    assert driver._build_verification_agent() is None


def test_driver_reports_the_verification_agents_spend_separately_from_the_agent_under_test(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        chat_agent_name="eval-todo-app",
        pre_events=[],
        turn_reply_events=[_reply_events("All done; open the preview.")],
    )
    expectations = parse_expectations(
        {"outcome": "A working to-do web app.", "deliverable": {"kind": "minds-app"}}, "todo-app"
    )

    _driver, _environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__verifier1",
        timeout_seconds=1800.0,
        expectations=expectations,
    )

    assert context.metadata is not None
    # Harness spend has its own key beside the decider's; the agent under test's cost fields must
    # never absorb the cost of measuring it.
    verifier_usage = context.metadata["verifier_agent_usage"]
    assert verifier_usage["model"] == _driver._decider_model
    assert verifier_usage["call_count"] == 0
    assert "verifier_agent_usage" in context.metadata and "decider_usage" in context.metadata
