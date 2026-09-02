import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from harbor.models.agent.context import AgentContext
from loguru import logger
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.minds_evals import decider
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CapturedFile
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import PromptEntry
from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.data_types import WorkerCapture
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.data_types import entry_exchange_budget
from imbue.minds_evals.driver import Done
from imbue.minds_evals.driver import FALLBACK_ENTRY_DETAIL
from imbue.minds_evals.driver import GoalTurnSource
from imbue.minds_evals.driver import LiteralTurnSource
from imbue.minds_evals.driver import MindsPersonaDriver
from imbue.minds_evals.driver import PersonaLLMTurnSource
from imbue.minds_evals.driver import Say
from imbue.minds_evals.driver import SnapshotMode
from imbue.minds_evals.driver import SnapshotPoint
from imbue.minds_evals.driver import TurnAction
from imbue.minds_evals.driver import TurnSource
from imbue.minds_evals.driver import _case_clone_dir
from imbue.minds_evals.driver import _embedded_workers
from imbue.minds_evals.driver import _new_agent_reply_texts
from imbue.minds_evals.driver import _settled_worker_count
from imbue.minds_evals.driver import build_case_clone_command
from imbue.minds_evals.driver import build_clone_probe_command
from imbue.minds_evals.driver import build_eval_base_clone_command
from imbue.minds_evals.driver import build_eval_case_commit_command
from imbue.minds_evals.driver import build_vendor_mngr_command
from imbue.minds_evals.driver import derive_user_id
from imbue.minds_evals.driver import is_snapshot_wanted
from imbue.minds_evals.driver import parse_agent_flag
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.driver import parse_snapshot_mode
from imbue.minds_evals.driver import resolve_turn_sources
from imbue.minds_evals.driver import sanitize_user_id
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import InstructionParseError
from imbue.minds_evals.expectations import expand_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.generate import oracle_entry_records
from imbue.minds_evals.mock_environment_test import ConversationModel
from imbue.minds_evals.mock_environment_test import MOCK_ACCOUNT_ID
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import failed_result
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result
from imbue.minds_evals.mock_turn_source_test import ScriptedSourceDriver
from imbue.minds_evals.mock_turn_source_test import ScriptedTurnSource
from imbue.minds_evals.mock_turn_source_test import done
from imbue.minds_evals.mock_turn_source_test import say
from imbue.minds_evals.testing import BOX_COMMON_TRANSCRIPT_PATH
from imbue.minds_evals.testing import BOX_WORKSPACE_TRAJECTORY_PATH
from imbue.minds_evals.testing import TEMPLATE_SUPERVISORD_CONF
from imbue.minds_evals.testing import WORKER_AGENT_ID
from imbue.minds_evals.testing import WORKER_NAME
from imbue.minds_evals.testing import WORKER_TASK_FILE
from imbue.minds_evals.testing import atif_document
from imbue.minds_evals.testing import atif_stream_jsonl
from imbue.minds_evals.testing import captured_transcript_downloads
from imbue.minds_evals.testing import commit_readme_revision
from imbue.minds_evals.testing import make_local_git_repo
from imbue.minds_evals.testing import program_block
from imbue.minds_evals.testing import transcript_capture_output
from imbue.minds_evals.testing import worker_capture_output
from imbue.minds_evals.testing import worker_document
from imbue.minds_evals.testing import worker_listing_json
from imbue.minds_evals.testing import worker_listing_output
from imbue.minds_evals.testing import worker_trial_downloads
from imbue.minds_evals.testing import workspace_state_output


def _case_config(
    prompts: tuple[PromptEntry, ...],
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
        expectations=expand_expectations(expectations) if expectations is not None else None,
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


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_case_config_rejects_a_blank_message_prompt(blank: str) -> None:
    """The non-empty bound rides the entry model, exactly as a goal entry's own bounds do, so a
    dataset the generator never vetted cannot make the client spend a full agent turn saying
    nothing."""
    blank_case = _case_config(("Build it",)).model_dump()
    blank_case["prompts"] = ["Build it", blank]
    instruction = "# Task\n\n```json\n{}\n```\n".format(json.dumps(blank_case, indent=2))

    with pytest.raises(ValidationError, match="at least 1 character"):
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


def test_resolve_turn_sources_gives_every_entry_its_own_source() -> None:
    case_config = _case_config(
        ("Build it", DECIDE_SENTINEL, GoalEntry(goal="Get a working preview link", max_exchanges=4), DECIDE_SENTINEL)
    )

    sources = resolve_turn_sources(case_config, "claude-opus-4-8", "")

    assert isinstance(sources[0], LiteralTurnSource)
    assert sources[0].prompt == "Build it"
    assert isinstance(sources[1], PersonaLLMTurnSource)
    assert isinstance(sources[2], GoalTurnSource)
    assert sources[2].goal == "Get a working preview link"
    assert isinstance(sources[3], PersonaLLMTurnSource)
    # Per-entry state (whether the entry has said its piece, which goal it holds) means a source
    # cannot be shared across entries.
    assert sources[1] is not sources[3]
    assert [source.kind for source in sources] == [
        TurnEntryKind.LITERAL,
        TurnEntryKind.PERSONA,
        TurnEntryKind.GOAL,
        TurnEntryKind.PERSONA,
    ]


def test_the_oracles_entry_kinds_are_the_ones_the_drivers_sources_report() -> None:
    """The oracle writes its state.json by hand, so its entry kinds are a second mapping from a
    prompts entry to a TurnEntryKind. A kind that disagreed with the source the driver would have
    built for the same entry would mislabel every oracle trial without failing anything -- the
    structural gate reads `outcome` and `exchange_count`, never `kind` -- in exactly the reference
    run real trials are compared against."""
    case_config = _case_config(("Build it", DECIDE_SENTINEL, GoalEntry(goal="See it running", max_exchanges=2)))

    oracle_kinds = [record["kind"] for record in oracle_entry_records(case_config)]

    assert oracle_kinds == [source.kind.value for source in resolve_turn_sources(case_config, "m", "")]


def test_entry_exchange_budget_is_one_for_a_string_entry_and_the_goals_own_ceiling() -> None:
    assert entry_exchange_budget("Build it") == 1
    assert entry_exchange_budget(DECIDE_SENTINEL) == 1
    assert entry_exchange_budget(GoalEntry(goal="Get a preview", max_exchanges=5)) == 5


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


def test_new_agent_reply_texts_reads_atif_agent_steps() -> None:
    """mngr's emitters write ATIF-shaped steps; the reply detector must see those turns too."""
    events = [
        {"type": "step", "source": "agent", "message": "old reply"},
        {"type": "step", "source": "user", "message": "our turn"},
        {"type": "step", "source": "agent", "message": ""},
        {"type": "observation", "results": [{"source_call_id": "c1", "content": "tool output"}]},
        {"type": "step", "source": "system", "message": "framework noise"},
        {"type": "step", "source": "agent", "message": "new reply"},
    ]

    assert _new_agent_reply_texts(events, 1) == ["new reply"]
    assert _new_agent_reply_texts(events, 6) == []


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


def _prepare_eval_base(tmp_path: Path) -> tuple[Path, Path, str]:
    """A local stand-in for the pinned workspace template, cloned the way clone prep clones it.

    Returns the eval-base clone (pinned to the SHA on a named branch, exactly as in the box), a
    per-case clone taken from it, and the pinned SHA.
    """
    source = make_local_git_repo(tmp_path, "fake-dwt", commit_count=1)
    pinned_sha = commit_readme_revision(source.repo_dir, "pinned\n", "pin")
    eval_base_dir = tmp_path / "eval-base"
    subprocess.run(
        [
            "bash",
            "-c",
            build_eval_base_clone_command(
                dwt_repo=str(source.repo_dir), dwt_branch="main", dwt_sha=pinned_sha, eval_base_dir=str(eval_base_dir)
            ),
        ],
        check=True,
        capture_output=True,
    )
    clone_dir = tmp_path / "case-clone"
    subprocess.run(["git", "clone", "-q", str(eval_base_dir), str(clone_dir)], check=True)
    return eval_base_dir, clone_dir, pinned_sha


def test_clone_prep_answers_both_shas_the_captured_bundle_is_replayed_from(tmp_path: Path) -> None:
    # Each SHA lands under its own section, and both are the pin: the per-case clone's HEAD is the
    # eval base's HEAD, which is what makes the bundle's base reproducible from the dwt tip.
    eval_base_dir, clone_dir, pinned_sha = _prepare_eval_base(tmp_path)

    result = subprocess.run(
        ["bash", "-c", build_clone_probe_command(str(clone_dir), str(eval_base_dir))],
        check=True,
        capture_output=True,
        text=True,
    )

    sections = evidence_collection.split_sections(result.stdout)
    assert sections["dwt_tip_sha"].strip() == pinned_sha
    assert sections["base_sha"].strip() == pinned_sha


def test_clone_prep_commands_shell_quote_every_box_path() -> None:
    # The case id is author-controlled and reaches four box commands, so each is pinned here with an
    # id that needs escaping. The box path constants render identically whether or not they are
    # quoted, so only a case-id path can catch a site left bare -- hence the spelled-out commands.
    assert build_case_clone_command(_case_clone_dir("todo app")) == (
        "mkdir -p /work/clones && rm -rf '/work/clones/todo app' && git clone /work/eval-base '/work/clones/todo app'"
    )
    assert build_clone_probe_command(_case_clone_dir("todo app"), "/work/eval-base") == (
        r"printf '<<<MINDS_EVALS_SECTION:base_sha>>>\n'; git -C '/work/clones/todo app' rev-parse HEAD; "
        r"printf '<<<MINDS_EVALS_SECTION:dwt_tip_sha>>>\n'; git -C /work/eval-base rev-parse HEAD"
    )
    vendor_command = build_vendor_mngr_command(_case_clone_dir("todo app"))
    assert vendor_command.startswith("mkdir -p '/work/clones/todo app'/system/vendor/mngr && ")
    # The trailing slash is rsync's "contents of", not "the directory itself".
    assert " /work/mngr/ '/work/clones/todo app'/system/vendor/mngr/" in vendor_command
    assert "cd '/work/clones/todo app' && " in build_eval_case_commit_command(
        _case_clone_dir("todo app"), "eval case todo app"
    )


def _write_modal_config(tmp_path: Path) -> Path:
    modal_config = tmp_path / "modal.toml"
    modal_config.write_text('[default]\ntoken_id = "ak-test"\ntoken_secret = "as-test"\nactive = true\n')
    return modal_config


def _setup_rules(
    is_boot_snapshot_failed: bool = False,
    transcript_capture: str = transcript_capture_output("0", "0", ""),
) -> list[ScriptedExecRule]:
    """The scripted box for everything except the stateful conversation endpoints (which the
    ConversationModel serves): boot, workspace create, clone prep, snapshot, and cleanup.

    ``is_boot_snapshot_failed`` makes the pre-turn-1 workspace-state probe fail at the bridge, the
    way a workspace whose exec path is not up yet fails it; the collection-time probe still answers.
    ``transcript_capture`` is what the in-workspace transcript capture prints; the default is a
    workspace whose mngr wrote both halves.
    """
    activation_script = (
        "# Activated env 'staging'.\n"
        "export MINDS_ROOT_NAME=minds-staging\n"
        "export MNGR_HOST_DIR=/root/.minds-staging/mngr\n"
        "export MNGR_PREFIX=minds-staging-\n"
        "unset MODAL_PROFILE\n"
    )

    # The same probe answers twice: the pre-turn-1 snapshot, then evidence collection. Only the
    # second lists `todo`, which is what makes it the delivered app. `terminal` is in the registry
    # but in no forward_port.py call -- see `resolve_preexisting_registrations`.
    template_registry = (
        '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n\n'
        '[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\n'
    )
    template_services = "system_interface   RUNNING   pid 101\nterminal   RUNNING   pid 102\n"
    boot_state = workspace_state_output(
        template_registry, services=template_services, supervisord=TEMPLATE_SUPERVISORD_CONF
    )
    delivered_state = workspace_state_output(
        template_registry + '\n[[apps]]\nname = "todo"\nurl = "http://localhost:8081"\nlabel = "todo-bb"\n',
        services=template_services + "todo   RUNNING   pid 103\n",
        supervisord=TEMPLATE_SUPERVISORD_CONF + program_block("todo", ("todo", "http://localhost:8081")),
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
        ScriptedExecRule(
            "MINDS_EVALS_SECTION:repo_root",
            [
                failed_result("mngr exec: workspace not reachable")
                if is_boot_snapshot_failed
                else ok_result(mngr_exec_json(boot_state)),
                ok_result(mngr_exec_json(delivered_state)),
            ],
        ),
        ScriptedExecRule("MINDS_EVALS_SECTION:stream_exit", [ok_result(mngr_exec_json(transcript_capture))]),
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


def _one_turn_conversation(
    reply_text: str = "Building it now.",
    is_first_create_answer_lost: bool = False,
    welcome_delay_polls: int = 0,
) -> ConversationModel:
    """A workspace whose chat answers a single turn: the shape tests take when the conversation is
    not what they are about. ``reply_text`` is flavour that keeps a trial readable -- no test that
    takes this shape asserts on it.

    Returns a fresh instance per call, since those tests go on to set the one attribute they *are*
    about (the chat's state, a refused create, a downed auth endpoint, the auth mode reported back).
    """
    return ConversationModel(
        chat_agent_id="chat-1",
        turn_reply_events=[_reply_events(reply_text)],
        is_first_create_answer_lost=is_first_create_answer_lost,
        welcome_delay_polls=welcome_delay_polls,
    )


def _box_trajectory(environment: MockBoxEnvironment) -> dict[str, Any]:
    """The trajectory.json the box holds, which is the copy harbor hands to the verifier."""
    return json.loads(environment.uploaded_content_by_target["/logs/agent/trajectory.json"])


def _assert_trajectory_is_hand_built(driver: MindsPersonaDriver, environment: MockBoxEnvironment) -> None:
    """trajectory.json, in the box and host-side alike, is the hand-built turn summary: one step per
    clean conversation turn, the driver as the agent, and the source marker to match."""
    trajectory = _box_trajectory(environment)
    assert [(step["source"], step["message"]) for step in trajectory["steps"]] == [
        (entry["role"], entry["text"]) for entry in driver._conversation if entry["text"].strip()
    ]
    assert trajectory["agent"] == {"name": "minds-persona-driver", "version": "0.1.0"}
    assert trajectory["extra"]["minds_evals"]["source"] == "hand_built"
    assert json.loads((driver.logs_dir / "trajectory.json").read_text()) == trajectory


def _proxy_rules(usage_log: str) -> list[ScriptedExecRule]:
    """What a box with a healthy in-box proxy answers: the liveness probe, the workspace's SSH
    endpoint for the reverse tunnel, the tunnel's readiness marker, and the metering log itself."""
    ssh_listing = json.dumps(
        {
            "agents": [
                {"id": "ws-1", "host": {"ssh": {"user": "root", "host": "1.2.3.4", "port": "22", "key_path": "/k"}}}
            ]
        }
    )
    return [
        ScriptedExecRule("health/liveliness", [ok_result("200")]),
        ScriptedExecRule("mngr list --format json", [ok_result(ssh_listing)]),
        ScriptedExecRule(minds_bridge.TUNNEL_LOG_FILENAME, [ok_result("TUNNEL_READY")]),
        ScriptedExecRule(minds_bridge.BOX_PROXY_USAGE_LOG_PATH, [ok_result(usage_log)]),
    ]


def _run_driver(
    tmp_path: Path,
    prompts: tuple[PromptEntry, ...],
    conversation: ConversationModel,
    trial_name: str,
    timeout_seconds: float,
    rules: list[ScriptedExecRule] | None = None,
    expectations: Expectations | None = None,
    is_proxy_enabled: bool = False,
    downloadable_content_by_source: dict[str, str] | None = None,
    rejected_upload_content_substring: str = "",
    scripted_sources: list[TurnSource] | None = None,
    snapshot_mode: str = "per-turn",
) -> tuple[MindsPersonaDriver, MockBoxEnvironment, AgentContext]:
    logs_dir = tmp_path / "jobs" / trial_name / "agent"
    logs_dir.mkdir(parents=True)
    # Shared by both arms below: a kwarg added to only one of them would change what half the suite
    # exercises without failing anything, since the two arms serve disjoint sets of tests.
    driver_kwargs: dict[str, Any] = {
        "logs_dir": logs_dir,
        "modal_config_path": str(_write_modal_config(tmp_path)),
        "poll_seconds": 0.01,
        "proxy": is_proxy_enabled,
        "snapshot_mode": snapshot_mode,
        # The key the driver signs the workspace in with, supplied the way harbor supplies it.
        "extra_env": {"ANTHROPIC_API_KEY": "sk-eval-test"},
    }
    driver = (
        MindsPersonaDriver(**driver_kwargs)
        if scripted_sources is None
        else ScriptedSourceDriver(scripted_sources, **driver_kwargs)
    )
    environment = MockBoxEnvironment(
        tmp_path, rules if rules is not None else _setup_rules(), conversation=conversation
    )
    environment.downloadable_content_by_source = dict(downloadable_content_by_source or {})
    environment.rejected_upload_content_substring = rejected_upload_content_substring
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

    # The trajectory in the box carries only the eval's own turns (no /welcome noise), one step
    # each, and is what the verifier grades.
    box_trajectory = _box_trajectory(environment)
    assert [(step["source"], step["message"]) for step in box_trajectory["steps"]] == [
        ("user", "Build it"),
        ("agent", "Building it now; I'll let you know when it's ready."),
        ("user", "Sounds good."),
        ("agent", "All done. Open the preview to try it out."),
    ]
    assert "/logs/agent/conversation.jsonl" not in environment.uploaded_content_by_target
    assert "/logs/agent/full_transcript.jsonl" not in environment.uploaded_content_by_target

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

    assert json.loads((driver.logs_dir / "trajectory.json").read_text()) == box_trajectory


def test_driver_signs_the_workspace_in_before_the_first_turn(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()
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


def test_driver_creates_the_chat_only_after_the_workspace_is_signed_in(tmp_path: Path) -> None:
    # A workspace boots with no chat, and a chat binds to a provider account when it is created --
    # so a create issued before sign-in is refused for want of an account, and the trial never gets
    # a chat to drive. The sign-in has to come first.
    conversation = _one_turn_conversation()
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__chat1",
        timeout_seconds=1800.0,
    )

    sign_in_index = next(
        index for index, command in enumerate(environment.exec_commands) if "submit-credentials" in command
    )
    create_index = next(index for index, command in enumerate(environment.exec_commands) if "create-chat" in command)
    assert sign_in_index < create_index
    # The chat is named after the workspace host and bound to the account the sign-in minted.
    assert len(conversation.create_chat_commands) == 1
    assert '"name": "EVAL-todo-app-chat1-' in conversation.create_chat_commands[0]
    assert conversation.chat_account_id == MOCK_ACCOUNT_ID
    # And the trial ran to the end on it.
    assert context.metadata is not None
    assert context.metadata["test_state"] == "finished"


def test_driver_drives_the_chat_it_already_created_when_a_retry_collides(tmp_path: Path) -> None:
    # A create whose answer is lost still made the chat, so the retry collides with it. The chat is
    # the one the trial should drive, so the collision is resolved from the agents listing rather
    # than failing a workspace that is perfectly usable.
    conversation = _one_turn_conversation(is_first_create_answer_lost=True)
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__chat2",
        timeout_seconds=1800.0,
    )

    assert len(conversation.create_chat_commands) == 2
    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "finished"
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 1


def test_driver_waits_out_the_welcome_before_sending_the_first_turn(tmp_path: Path) -> None:
    # A freshly created chat is listed as WAITING before the workspace has typed its `/welcome` in,
    # and that window is wide (the workspace waits for the agent's TUI first). A driver that took
    # the first WAITING for "ready" would send turn 1 into it and then read the welcome greeting --
    # the next agent message to arrive -- as the answer to turn 1.
    conversation = _one_turn_conversation(welcome_delay_polls=4)
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__chat5",
        timeout_seconds=1800.0,
    )

    # The reply graded for turn 1 is the turn's own reply, with no trace of the greeting in it.
    assert [(step["source"], step["message"]) for step in _box_trajectory(environment)["steps"]] == [
        ("user", "Build it"),
        ("agent", "Building it now."),
    ]
    # Which is because nothing was sent until the welcome had been waited out.
    send_index = next(index for index, command in enumerate(environment.exec_commands) if "/message" in command)
    welcome_polls_before_send = len(
        [command for command in environment.exec_commands[:send_index] if "/events?offset=0&limit=1" in command]
    )
    assert welcome_polls_before_send > 4
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 1


def test_driver_marks_timed_out_when_the_created_chat_never_becomes_ready(tmp_path: Path) -> None:
    # A chat that never reaches WAITING is one no turn can be sent to, so the trial stops at the
    # gate rather than sending into a chat that is not listening and grading the silence.
    conversation = _one_turn_conversation()
    conversation.chat_state = "STARTING"
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__chat4",
        # This test asserts the trial got as far as creating the chat, so the whole bring-up has to
        # fit inside the deadline rather than merely being allowed to. The chat is pinned to
        # STARTING, so the deadline still ends it.
        timeout_seconds=2.0,
    )

    assert conversation.is_chat_created
    assert not any("/message" in command for command in environment.exec_commands)
    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 0


def test_driver_marks_timed_out_when_the_chat_cannot_be_created(tmp_path: Path) -> None:
    # A refused create is the workspace's own answer, not a workspace still coming up: the trial
    # stops there rather than spending its budget on a chat that will never exist.
    conversation = _one_turn_conversation()
    conversation.create_chat_refusal_detail = "no provider accounts exist yet"
    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__chat3",
        timeout_seconds=1800.0,
    )

    assert len(conversation.create_chat_commands) == 1
    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert context.metadata is not None
    assert context.metadata["turns_completed"] == 0


def test_driver_marks_timed_out_when_the_workspace_cannot_be_signed_in(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()
    conversation.is_auth_endpoint_up = False
    driver, environment, context = _run_driver(
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
    # No exchange happened, so there is no conversation to describe: no trajectory at all rather
    # than a hand-built one with no steps.
    assert context.metadata["trajectory_source"] == "none"
    assert not (driver.logs_dir / "trajectory.json").exists()


def test_driver_reports_the_workspace_agents_usage_and_keeps_the_decider_separate(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
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


def test_driver_reports_the_proxys_account_everywhere_when_a_proxy_metered_the_trial(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        turn_reply_events=[
            _reply_events(
                "Delegating the build.",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_tokens": 5_000,
                    "cache_write_tokens": 2_000,
                },
            )
        ],
    )
    # Behind the proxy the workspace is signed in with a key plus a base URL, which the product
    # reports as the "imbue" auth mode rather than a bare api_key.
    conversation.expected_auth_mode = minds_bridge.AUTH_MODE_IMBUE
    # The proxy sees delegated calls the transcript never does, so its totals are strictly larger --
    # which is what makes it detectable when a consumer reads the wrong source.
    proxy_log = "\n".join(
        json.dumps(record)
        for record in (
            {
                "model": "claude-opus-4-8",
                "input_tokens": 40,
                "output_tokens": 400,
                "cache_read_tokens": 9_000,
                "cache_write_tokens": 3_000,
                "speed": None,
            },
            {
                "model": "claude-opus-4-8",
                "input_tokens": 7,
                "output_tokens": 70,
                "cache_read_tokens": 1_000,
                "cache_write_tokens": 0,
                "speed": None,
            },
        )
    )
    driver, _environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__proxy1",
        timeout_seconds=1800.0,
        rules=_proxy_rules(proxy_log) + _setup_rules(),
        is_proxy_enabled=True,
    )

    assert context.metadata is not None
    assert context.metadata["usage_source"] == "proxy"
    # The transcript is still recorded, and still disagrees -- so the assertions below are about
    # which source was chosen, not about the two happening to match.
    assert context.metadata["transcript_usage"]["tokens"]["output"] == 100
    assert context.n_output_tokens == 470
    assert context.n_cache_tokens == 10_000
    assert context.n_input_tokens == 47 + 10_000 + 3_000
    assert context.cost_usd is not None and context.cost_usd > 0

    # Harbor's own fields, the usage artifact, and the trajectory all describe one trial.
    usage_artifact = json.loads((driver.logs_dir / "usage.json").read_text())
    assert usage_artifact["workspace_agent"]["cost_usd"] == context.cost_usd
    final_metrics = json.loads((driver.logs_dir / "trajectory.json").read_text())["final_metrics"]
    assert final_metrics["total_completion_tokens"] == context.n_output_tokens
    assert final_metrics["total_cached_tokens"] == context.n_cache_tokens
    assert final_metrics["total_prompt_tokens"] == context.n_input_tokens
    assert final_metrics["total_cost_usd"] == context.cost_usd


def test_driver_leaves_usage_unset_when_the_transcript_carries_none(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()
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
    # The per-turn hand-built trajectory is already in the box, so the verifier still has a record
    # of the exchange that did happen: the client's turn, with no reply to it.
    assert [(step["source"], step["message"]) for step in _box_trajectory(environment)["steps"]] == [
        ("user", "Build it")
    ]
    assert context.metadata is not None
    assert context.metadata["timed_out"] is True
    # It is the reply that is missing, not the bring-up: state.json records no reason for a
    # timeout, so without this the assertions above would pass for a trial that never got a chat.
    assert any("/message" in command for command in environment.exec_commands)
    # Cleanup still ran.
    assert any("mngr destroy - --force" in command for command in environment.exec_commands)


def test_driver_fails_when_the_workspace_reports_the_wrong_auth_mode(tmp_path: Path) -> None:
    # The sign-in endpoint runs no credential probe, so it accepts a bad key and reports the mode it
    # ended up in. Without checking that, the trial would run on an unauthenticated workspace and
    # the judge would grade the agent's "not logged in" replies as if they were its own behaviour.
    conversation = _one_turn_conversation()
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
    conversation = _one_turn_conversation(reply_text="All done; open the preview.")
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

    # Only the app the agent added is scored; the terminal was already serving before turn 1, and
    # only the registry half catches it (see `resolve_preexisting_registrations`).
    assert "terminal" in manifest["preexisting_registrations"]
    assert "todo" not in manifest["preexisting_registrations"]
    assert "app_registered_service_terminal" not in statuses
    assert "http_0_registered_apps_terminal" not in statuses

    # The bundle's base is the prepared clone's HEAD, so only the agent's own commits are captured.
    repo_state = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/repo_state.json"])
    assert repo_state["base_sha"] == "c" * 40
    assert repo_state["head_sha"] == "d" * 40
    # The dwt tip travels too, so a replay can regenerate the base and check it reproduces base_sha.
    assert repo_state["dwt_tip_sha"] == "e" * 40
    assert manifest["base_sha"] == "c" * 40
    assert manifest["dwt_tip_sha"] == "e" * 40

    # Collection happens while the workspace is alive: before the destroy sweep, never after.
    # The bundle capture is the collector's alone, unlike the workspace-state probe (which also
    # answers the pre-turn-1 snapshot), so it dates the collection phase unambiguously.
    collection_index = next(
        index for index, command in enumerate(environment.exec_commands) if "git bundle create" in command
    )
    destroy_index = next(
        index for index, command in enumerate(environment.exec_commands) if "mngr destroy - --force" in command
    )
    assert collection_index < destroy_index

    assert context.metadata is not None
    assert context.metadata["verification"]["is_evidence_complete"] is True


def test_driver_publishes_the_workspace_trajectory_for_grading(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
        turn_reply_events=[
            _reply_events(
                "Building it now.",
                usage={"input_tokens": 10, "output_tokens": 40, "cache_read_tokens": 1_000, "cache_write_tokens": 0},
            )
        ],
    )

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__atif1",
        timeout_seconds=1800.0,
        downloadable_content_by_source=captured_transcript_downloads(),
    )

    # trajectory.json is the workspace's document -- its steps, tool calls and embedded subagent --
    # carrying the trial's resolved usage and the eval's provenance, in the box for the verifier
    # and host-side alike.
    trajectory = _box_trajectory(environment)
    document = atif_document()
    assert trajectory["steps"] == document["steps"]
    assert trajectory["subagent_trajectories"] == document["subagent_trajectories"]
    assert trajectory["agent"] == {"name": "claude", "version": "unknown"}
    assert trajectory["final_metrics"] == {
        "total_prompt_tokens": 1_010,
        "total_completion_tokens": 40,
        "total_cached_tokens": 1_000,
        "total_cost_usd": context.cost_usd,
        "total_steps": 2,
    }
    assert trajectory["extra"]["minds_evals"]["source"] == "workspace"
    assert trajectory["extra"]["minds_evals"]["decider_model"] == "claude-opus-4-8"
    assert trajectory["extra"]["minds_evals"]["usage_source"] == "transcript"
    assert json.loads((driver.logs_dir / "trajectory.json").read_text()) == trajectory
    # The captured stream stays in the bundle as evidence; nothing UI-feed-shaped is an artifact.
    assert (driver.logs_dir / "verification" / "common_transcript.jsonl").read_text() == atif_stream_jsonl()
    assert "/logs/agent/conversation.jsonl" not in environment.uploaded_content_by_target
    assert "/logs/agent/full_transcript.jsonl" not in environment.uploaded_content_by_target

    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "workspace"
    assert context.metadata["transcript_capture"]["stream"]["is_captured"] is True
    assert context.metadata["transcript_capture"]["document"]["is_captured"] is True
    # The usage account still comes from what the driver polled during the conversation.
    assert context.metadata["transcript_usage"]["message_count"] == 1


def _worker_rules(capture_output: str, listing_json: str = worker_listing_json("WAITING")) -> list[ScriptedExecRule]:
    """The scripted box for a trial whose chat agent launched the worker: the listing, the worker's
    capture, and everything `_setup_rules` scripts."""
    return [
        ScriptedExecRule(
            "MINDS_EVALS_SECTION:list_exit", [ok_result(mngr_exec_json(worker_listing_output(listing_json)))]
        ),
        ScriptedExecRule("mngr transcript {}".format(WORKER_NAME), [ok_result(mngr_exec_json(capture_output))]),
        *_setup_rules(),
    ]


def test_driver_embeds_a_launched_worker_under_its_launching_call(tmp_path: Path) -> None:
    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        _one_turn_conversation(),
        trial_name="todo-app__worker1",
        timeout_seconds=1800.0,
        rules=_worker_rules(worker_capture_output("0", "0", "", WORKER_TASK_FILE, "")),
        downloadable_content_by_source=worker_trial_downloads(),
    )

    trajectory = _box_trajectory(environment)
    # The worker sits under the call that launched it, as one of the document's embedded subagents.
    assert [entry["trajectory_id"] for entry in trajectory["subagent_trajectories"]] == ["sub-1", WORKER_AGENT_ID]
    launch_result = trajectory["steps"][2]["observation"]["results"][0]
    assert launch_result["subagent_trajectory_ref"][0]["trajectory_id"] == WORKER_AGENT_ID
    worker = trajectory["subagent_trajectories"][1]
    assert worker["extra"]["worker"]["name"] == WORKER_NAME
    assert worker["extra"]["worker"]["report_path"] == "verification/workers/{}/reports".format(WORKER_NAME)
    assert (driver.logs_dir / "verification" / "workers" / WORKER_NAME / "reports" / "report.md").exists()

    # The worker's own inference is priced into the trial's transcript account, and the account is
    # complete because every launch was captured.
    assert context.metadata is not None
    transcript_usage = context.metadata["transcript_usage"]
    assert transcript_usage["worker_launch_count"] == 1
    assert transcript_usage["worker_captured_count"] == 1
    assert transcript_usage["is_cost_complete"] is True
    # The mock's polled reply carries no usage, so the worker's one inference is the only priced message.
    assert transcript_usage["message_count"] == 1
    assert transcript_usage["tokens"]["output"] == 60
    assert trajectory["final_metrics"]["total_completion_tokens"] == transcript_usage["tokens"]["output"]
    workers = context.metadata["workers"]
    assert [
        (entry["name"], entry["state"], entry["document"]["is_captured"], entry["report"]["is_captured"])
        for entry in workers
    ] == [(WORKER_NAME, "stopped", True, True)]
    # The worker's own account is reported beside it, so the delegated spend can be reconciled per
    # worker against a proxy's figures.
    assert (workers[0]["usage"]["message_count"], workers[0]["usage"]["tokens"]["output"]) == (1, 60)
    assert context.metadata["worker_capture_overflow"] == []


def test_driver_builds_a_listed_workers_trajectory_from_its_stream_as_its_own_type(tmp_path: Path) -> None:
    # The worker's document failed to build in the workspace, so the driver builds it host-side
    # from the stream, enriched with the type the listing reported rather than the default.
    listing = json.loads(worker_listing_json("WAITING"))
    listing["agents"][1]["type"] = "codex"

    _driver, environment, _context = _run_driver(
        tmp_path,
        ("Build it",),
        _one_turn_conversation(),
        trial_name="todo-app__worker2",
        timeout_seconds=1800.0,
        rules=_worker_rules(
            worker_capture_output("1", "0", "", WORKER_TASK_FILE, "unusable stream"), listing_json=json.dumps(listing)
        ),
        downloadable_content_by_source=worker_trial_downloads(is_document_included=False),
    )

    worker = _box_trajectory(environment)["subagent_trajectories"][1]
    assert (worker["trajectory_id"], worker["agent"]["name"]) == (WORKER_AGENT_ID, "codex")
    assert [step["message"] for step in worker["steps"]] == [
        "Harden the todo app and report back.",
        "Hardened; report pushed.",
    ]


def test_driver_rebuilds_an_invalid_worker_document_from_its_stream_and_keeps_the_root(tmp_path: Path) -> None:
    # The workspace wrote a worker document that is not valid ATIF: the worker is rebuilt from its
    # stream, and the root document is still the workspace's rather than the hand-built fallback.
    downloads = worker_trial_downloads()
    downloads["/logs/agent/verification/workers/{}/trajectory.json".format(WORKER_NAME)] = json.dumps(
        {**worker_document(WORKER_AGENT_ID), "steps": []}
    )

    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        _one_turn_conversation(),
        trial_name="todo-app__worker3",
        timeout_seconds=1800.0,
        rules=_worker_rules(worker_capture_output("0", "0", "", WORKER_TASK_FILE, "")),
        downloadable_content_by_source=downloads,
    )

    trajectory = _box_trajectory(environment)
    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "workspace"
    worker = trajectory["subagent_trajectories"][1]
    assert worker["trajectory_id"] == WORKER_AGENT_ID
    assert [step["message"] for step in worker["steps"]] == [
        "Harden the todo app and report back.",
        "Hardened; report pushed.",
    ]


def test_settled_worker_count_counts_only_settled_workers_whose_streams_were_read(tmp_path: Path) -> None:
    stream_path = tmp_path / "common_transcript.jsonl"
    stream_path.write_text("")
    captured_stream = CapturedFile(host_path=stream_path, failure_reason="", failure_detail="")

    def _capture(name: str, state: WorkerState) -> WorkerCapture:
        return WorkerCapture(
            launch=WorkerLaunch(name=name, tool_call_id="c-" + name, task_file="", depth=0, lead_name=""),
            agent_id="agent-" + name,
            agent_type="claude",
            state=state,
            document=captured_stream,
            stream=captured_stream,
            report=captured_stream,
        )

    captures = [
        _capture("settled", WorkerState.STOPPED),
        _capture("destroyed", WorkerState.DESTROYED),
        _capture("running", WorkerState.RUNNING),
        # Nothing established whether it had settled, so it is treated like a running one.
        _capture("unknown", WorkerState.UNKNOWN),
        # Captured, but its stream never made it into the account.
        _capture("unread", WorkerState.STOPPED),
    ]
    records_by_name: dict[str, list[dict[str, Any]]] = {
        "settled": [],
        "destroyed": [],
        "running": [],
        "unknown": [],
    }

    assert _settled_worker_count(captures, records_by_name) == 2


def test_embedded_workers_leaves_out_a_workers_worker_whose_lead_could_not_be_embedded(tmp_path: Path) -> None:
    # The lead's document and stream both failed to come out; its own worker's document is sound,
    # and so is that worker's worker's, but a nested worker embeds inside its lead's document, so
    # neither has anywhere to go -- and each is said to be left out, the deeper one included, whose
    # own lead was embeddable.
    uncaptured = CapturedFile(
        host_path=None, failure_reason=evidence_collection.REASON_TRANSCRIPT_COMMAND_FAILED, failure_detail=""
    )

    def _captured_document(name: str, agent_id: str) -> CapturedFile:
        document_path = tmp_path / "{}.json".format(name)
        document_path.write_text(json.dumps(worker_document(agent_id)))
        return CapturedFile(host_path=document_path, failure_reason="", failure_detail="")

    captures = [
        WorkerCapture(
            launch=WorkerLaunch(name="lead", tool_call_id="c1", task_file="", depth=0, lead_name=""),
            agent_id="",
            agent_type="",
            state=WorkerState.UNKNOWN,
            document=uncaptured,
            stream=uncaptured,
            report=uncaptured,
        ),
        WorkerCapture(
            launch=WorkerLaunch(name="nested", tool_call_id="c2", task_file="", depth=1, lead_name="lead"),
            agent_id=WORKER_AGENT_ID,
            agent_type="claude",
            state=WorkerState.STOPPED,
            document=_captured_document("nested", WORKER_AGENT_ID),
            stream=uncaptured,
            report=uncaptured,
        ),
        WorkerCapture(
            launch=WorkerLaunch(name="deeper", tool_call_id="c3", task_file="", depth=2, lead_name="nested"),
            agent_id="agent-deeper",
            agent_type="claude",
            state=WorkerState.STOPPED,
            document=_captured_document("deeper", "agent-deeper"),
            stream=uncaptured,
            report=uncaptured,
        ),
    ]
    logged: list[str] = []
    handler_id = logger.add(lambda message: logged.append(message.record["message"]), level="WARNING")
    try:
        embedded = _embedded_workers(captures, tmp_path)
    finally:
        logger.remove(handler_id)

    assert embedded == []
    assert [line for line in logged if "is not embedded" in line] == [
        "Worker lead is not embedded in the trajectory: its stream was not captured",
        "Worker nested is not embedded in the trajectory: its lead lead was not",
        "Worker deeper is not embedded in the trajectory: its lead nested was not",
    ]


def test_driver_grades_on_the_hand_built_trajectory_when_the_transcript_commands_fail(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()
    # A workspace with no `mngr` on its exec path: neither half comes out.
    stderr = "sh: 1: mngr: not found"

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__nomngr1",
        timeout_seconds=1800.0,
        rules=_setup_rules(transcript_capture=transcript_capture_output("127", "127", stderr)),
    )

    _assert_trajectory_is_hand_built(driver, environment)
    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "hand_built"
    capture = context.metadata["transcript_capture"]
    assert capture["stream"]["reason"] == "transcript_command_failed"
    assert capture["document"]["reason"] == "transcript_command_failed"
    assert stderr in capture["document"]["detail"]


def test_driver_grades_on_the_hand_built_trajectory_for_a_pre_atif_workspace(tmp_path: Path) -> None:
    # A mngr that predates ATIF answers `--format jsonl` with the legacy-shaped records its emitter
    # wrote but knows no `--format atif`: the stream lands in the bundle as evidence, and grading
    # gets the hand-built document.
    conversation = _one_turn_conversation()
    stderr = "Error: Invalid value for '--format': 'atif' is not one of 'human', 'json', 'jsonl'."
    legacy_stream = '{"type": "user_message", "content": "Build it"}\n'

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__legacy1",
        timeout_seconds=1800.0,
        rules=_setup_rules(transcript_capture=transcript_capture_output("0", "2", stderr)),
        downloadable_content_by_source={BOX_COMMON_TRANSCRIPT_PATH: legacy_stream},
    )

    _assert_trajectory_is_hand_built(driver, environment)
    assert (driver.logs_dir / "verification" / "common_transcript.jsonl").read_text() == legacy_stream
    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "hand_built"
    capture = context.metadata["transcript_capture"]
    assert capture["stream"]["is_captured"] is True
    assert capture["document"]["reason"] == "transcript_command_failed"
    assert stderr in capture["document"]["detail"]


def test_driver_keeps_the_per_turn_trajectory_when_the_final_upload_cannot_reach_the_box(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__publish1",
        timeout_seconds=1800.0,
        downloadable_content_by_source=captured_transcript_downloads(),
        # Only the workspace document carries the workspace's own extra, so only its upload fails.
        rejected_upload_content_substring="workspace_note",
    )

    # The box still holds the last per-turn copy, the host copy is put back to match it, and the
    # metadata describes what the verifier will actually read.
    _assert_trajectory_is_hand_built(driver, environment)
    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "hand_built"
    # The document did reach the bundle; it is the final publish that failed.
    assert context.metadata["transcript_capture"]["document"]["is_captured"] is True


def test_driver_writes_no_trajectory_when_the_only_document_cannot_reach_the_box(tmp_path: Path) -> None:
    # The chat exists but never becomes ready, so no turn is exchanged and no per-turn copy is in the
    # box; the workspace's own document was captured but its upload fails. Nothing can stand in for
    # it, so the host copy goes too rather than describing a document the verifier never got.
    conversation = _one_turn_conversation()
    conversation.chat_state = "STARTING"

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__publish2",
        timeout_seconds=2.0,
        downloadable_content_by_source=captured_transcript_downloads(),
        rejected_upload_content_substring="workspace_note",
    )

    assert context.metadata is not None
    assert context.metadata["transcript_capture"]["document"]["is_captured"] is True
    assert context.metadata["trajectory_source"] == "none"
    assert "/logs/agent/trajectory.json" not in environment.uploaded_content_by_target
    assert not (driver.logs_dir / "trajectory.json").exists()


def test_driver_grades_on_the_hand_built_trajectory_when_the_captured_document_is_unusable(tmp_path: Path) -> None:
    conversation = _one_turn_conversation()
    # The document came out of the workspace but is not valid ATIF (a trajectory needs a step).
    stepless_document = json.dumps({**atif_document(), "steps": []})

    driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__unusable1",
        timeout_seconds=1800.0,
        downloadable_content_by_source={
            BOX_COMMON_TRANSCRIPT_PATH: atif_stream_jsonl(),
            BOX_WORKSPACE_TRAJECTORY_PATH: stepless_document,
        },
    )

    _assert_trajectory_is_hand_built(driver, environment)
    assert context.metadata is not None
    assert context.metadata["trajectory_source"] == "hand_built"
    # The document did reach the bundle; it was refused host-side, not lost in transit.
    assert context.metadata["transcript_capture"]["document"]["is_captured"] is True


def test_driver_leaves_the_delivered_apps_unmeasured_when_the_boot_snapshot_fails(tmp_path: Path) -> None:
    # Without the pre-turn-1 snapshot nothing in the registry can be told apart from what the
    # workspace booted with. That is the instrument failing, so the trial still runs to the end and
    # every check that depends on the distinction is recorded as unmeasured -- never as a failure
    # that charges the agent for the template's own apps, and never as an empty set that would
    # credit the agent with them.
    conversation = _one_turn_conversation(reply_text="All done; open the preview.")
    expectations = parse_expectations(
        {"outcome": "A working to-do web app.", "deliverable": {"kind": "minds-app"}}, "todo-app"
    )

    _driver, environment, context = _run_driver(
        tmp_path,
        ("Build it",),
        conversation,
        trial_name="todo-app__boot1",
        timeout_seconds=1800.0,
        rules=_setup_rules(is_boot_snapshot_failed=True),
        expectations=expectations,
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "finished"

    manifest = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/manifest.json"])
    assert manifest["preexisting_registrations"] is None
    entries = {entry["entry_id"]: entry for entry in manifest["entries"]}
    assert (entries["app_registered"]["status"], entries["app_registered"]["reason"]) == (
        "error",
        evidence_collection.REASON_PREEXISTING_UNKNOWN,
    )
    assert entries["http_0_registered_apps"]["reason"] == evidence_collection.REASON_PREEXISTING_UNKNOWN
    assert not any(entry["status"] == "failed" for entry in manifest["entries"])
    assert manifest["is_evidence_complete"] is False
    # The registry is still captured verbatim, so the trial stays diagnosable after the fact.
    assert 'name = "todo"' in environment.uploaded_content_by_target["/logs/agent/verification/apps.toml"]
    # The verdict reaches the trial's metadata as unmeasured, so a grader sees a harness gap rather
    # than a clean bill of health.
    assert context.metadata is not None
    assert context.metadata["verification"]["is_evidence_complete"] is False


def test_driver_skips_the_expectation_probes_on_a_timed_out_trial(tmp_path: Path) -> None:
    conversation = ConversationModel(
        chat_agent_id="chat-1",
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
    conversation = _one_turn_conversation(reply_text="Here is what I can do.")

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
    command = build_eval_case_commit_command("/work/clones/todo-app", "eval case todo-app")

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
            ["bash", "-c", build_eval_case_commit_command(str(repo), "eval case todo-app")],
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

    assert any(event.get("text") == "tidying up" for event in driver._latest_events)
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
    conversation = _one_turn_conversation(reply_text="All done; open the preview.")
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


def _goal_conversation(reply_texts: tuple[str, ...]) -> ConversationModel:
    """A workspace that answers each client message with the next of the given replies."""
    return ConversationModel(
        chat_agent_id="chat-1",
        turn_reply_events=[_reply_events(text) for text in reply_texts],
    )


# The opening ask every goal case below commissions the work with. A literal first entry is the
# config rule, so no goal test varies it.
_OPENING_PROMPT: Final[str] = "Build it"


def _run_goal_driver(
    tmp_path: Path,
    trial_name: str,
    goal: str,
    max_exchanges: int,
    actions: list[TurnAction],
    replies: tuple[str, ...],
    is_decider_call_simulated: bool = False,
    snapshot_mode: str = "per-turn",
    timeout_seconds: float = 1800.0,
    refused_send_index: int | None = None,
) -> tuple[ScriptedTurnSource, MockBoxEnvironment, AgentContext]:
    """Drive a two-entry case -- the opening literal ask, then one scripted goal entry -- through the
    real conversation loop, and hand back the goal source so a test can assert on what it was asked.

    A tiny `timeout_seconds` plus a short `replies` (or a `refused_send_index`) is how the timing-out
    paths are reached: the trial's budget expires on a reply or a send that never lands.
    """
    goal_source = ScriptedTurnSource(
        actions=actions,
        entry_kind=TurnEntryKind.GOAL,
        budget_outcome=TurnOutcome.BUDGET_EXHAUSTED,
        is_decider_call_simulated=is_decider_call_simulated,
    )
    sources: list[TurnSource] = [LiteralTurnSource(prompt=_OPENING_PROMPT), goal_source]
    conversation = _goal_conversation(replies)
    conversation.refused_send_index = refused_send_index
    _driver, environment, context = _run_driver(
        tmp_path,
        (_OPENING_PROMPT, GoalEntry(goal=goal, max_exchanges=max_exchanges)),
        conversation,
        trial_name=trial_name,
        timeout_seconds=timeout_seconds,
        scripted_sources=sources,
        snapshot_mode=snapshot_mode,
    )
    return goal_source, environment, context


def _decider_audit_events(environment: MockBoxEnvironment) -> list[dict[str, Any]]:
    """The decider calls the driver recorded in the trajectory's provenance block, in order."""
    document = json.loads(environment.uploaded_content_by_target["/logs/agent/trajectory.json"])
    return document["extra"]["minds_evals"]["decider_turns"]


def _client_messages(environment: MockBoxEnvironment) -> list[str]:
    """The client's turns as the trajectory in the box records them."""
    document = json.loads(environment.uploaded_content_by_target["/logs/agent/trajectory.json"])
    return [step["message"] for step in document["steps"] if step["source"] == "user"]


def test_driver_keeps_exchanging_within_one_goal_entry_until_the_client_is_satisfied(tmp_path: Path) -> None:
    goal_source, environment, context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal1",
        goal="See the app running",
        max_exchanges=4,
        actions=[say("Where is it?"), say("Can I see it?"), done(TurnOutcome.SATISFIED, "It is running.")],
        replies=("Building it.", "Nearly there.", "Here is the link."),
        is_decider_call_simulated=True,
    )

    # Three client messages out of two configured entries: the goal entry expanded into two.
    assert _client_messages(environment) == ["Build it", "Where is it?", "Can I see it?"]

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "finished"
    # The messages sent and the entries configured are different counts because a goal entry can
    # send several; the ported schema key carries the entry count.
    assert state["waits_done"] == 3
    assert state["num_turns"] == 2
    assert state["entries"] == [
        {"index": 0, "kind": "literal", "exchange_count": 1, "outcome": "completed", "detail": ""},
        {"index": 1, "kind": "goal", "exchange_count": 2, "outcome": "satisfied", "detail": "It is running."},
    ]
    # The client decided each exchange from the conversation alone, and each decision saw the reply
    # to the message before it -- nothing here reaches into the environment.
    assert goal_source.seen_conversations == [
        "Build it | Building it.",
        "Build it | Building it. | Where is it? | Nearly there.",
        "Build it | Building it. | Where is it? | Nearly there. | Can I see it? | Here is the link.",
    ]
    assert context.metadata is not None
    assert context.metadata["entries"] == state["entries"]


def test_driver_stops_a_goal_entry_at_its_budget_and_records_it_as_exhausted(tmp_path: Path) -> None:
    """The LOOP enforces max_exchanges: a source that would keep asking forever is cut off, and the
    entry is recorded rather than failing the trial."""
    goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal2",
        goal="Get it working",
        max_exchanges=2,
        actions=[say("Still not working.")],
        replies=("Building it.", "Nearly.", "Almost.", "Any moment."),
    )

    # The source would have said the same thing forever; it was asked exactly twice.
    assert goal_source.call_count == 2
    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "finished"
    assert state["waits_done"] == 3
    assert state["entries"][1] == {
        "index": 1,
        "kind": "goal",
        "exchange_count": 2,
        "outcome": "budget_exhausted",
        "detail": "",
    }


def test_driver_records_a_degraded_goal_entry_as_a_fallback(tmp_path: Path) -> None:
    """The loop's half of the degraded path: a source that ends its entry with FALLBACK is recorded
    as one and leaves the rest of its budget unspent. What makes a real client degrade is the
    source's own business, covered by the two tests that drive a real `GoalTurnSource`."""
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal3",
        goal="Get it working",
        max_exchanges=5,
        actions=[say(decider.FALLBACK_MESSAGE), done(TurnOutcome.FALLBACK)],
        replies=("Building it.", "Done."),
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    # One of the five allowed exchanges was spent; the FALLBACK ending stopped the entry there.
    assert state["entries"][1] == {
        "index": 1,
        "kind": "goal",
        "exchange_count": 1,
        "outcome": "fallback",
        "detail": "",
    }
    assert state["waits_done"] == 2


def test_driver_stamps_every_decider_call_with_its_entry_and_exchange(tmp_path: Path) -> None:
    _goal_source, environment, context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal4",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?"), say("And now?"), done(TurnOutcome.SATISFIED, "I can see it running.")],
        replies=("Building it.", "Nearly.", "Here."),
        is_decider_call_simulated=True,
    )

    audit_events = _decider_audit_events(environment)
    # The call that ended the entry is billed and audited like the ones that spoke, but it carries no
    # turn number: turns 2 and 3 belong to the messages that were actually sent, and handing the
    # decision the next one would hand that number to two events.
    assert [(event["entry_index"], event["exchange"], event["turn"]) for event in audit_events] == [
        (1, 0, 2),
        (1, 1, 3),
        (1, 2, None),
    ]
    assert [event["detail"] for event in audit_events] == ["", "", "I can see it running."]
    assert {event["entry_kind"] for event in audit_events} == {"goal"}
    # The goal client's spend is the simulated user's, so it lands in the decider bucket.
    assert context.metadata is not None
    assert context.metadata["decider_usage"]["call_count"] == 3


def test_driver_audits_a_message_that_went_out_but_drew_no_reply_as_sent(tmp_path: Path) -> None:
    """A decider call's audit event is stamped with the message it produced only once that message
    has reached the workspace -- but reaching the workspace is what counts, not the agent replying.
    The commonest timeout is a message that went out and was never answered; it is in the
    trajectory and it owns a turn number, so the audit record has to agree."""
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal9",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?")],
        # One reply for the opening ask and nothing for the goal entry's message, so the trial's tiny
        # budget expires waiting for a reply that never comes.
        replies=("Building it.",),
        is_decider_call_simulated=True,
        timeout_seconds=0.3,
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert state["waits_done"] == 2
    # The entry the trial died in earns no record, so the records account for one of the two
    # messages sent. Only a finished trial's two views of the conversation agree.
    assert state["entries"] == [
        {"index": 0, "kind": "literal", "exchange_count": 1, "outcome": "completed", "detail": ""}
    ]
    assert _client_messages(environment) == ["Build it", "Where is it?"]
    assert [event["turn"] for event in _decider_audit_events(environment)] == [2]


def test_driver_audits_a_message_that_never_reached_the_workspace_as_unsent(tmp_path: Path) -> None:
    """The client's decision is made before the workspace is touched, so a call can be billed for a
    message that the send then fails to deliver. Such a call owns no turn number: the conversation
    never had that turn, and `waits_done` -- which the gates read -- never counted it."""
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal10",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?")],
        replies=("Building it.", "Nearly."),
        is_decider_call_simulated=True,
        timeout_seconds=0.3,
        # The goal entry's message is the second the run sends, and it is the one the workspace
        # never accepts, so the trial's tiny budget expires retrying it.
        refused_send_index=2,
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["test_state"] == "timed_out"
    assert state["waits_done"] == 1
    # Still audited and still billed -- the model call happened and cost money.
    assert _client_messages(environment) == ["Build it"]
    assert [event["turn"] for event in _decider_audit_events(environment)] == [None]


def test_driver_records_the_clients_own_reason_for_ending_a_goal_entry(tmp_path: Path) -> None:
    """`satisfied` says the client stopped asking; the reason it gave is what tells a reader of a
    captured trial why, without re-reading the whole transcript."""
    _goal_source, environment, context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal6",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?"), done(TurnOutcome.SATISFIED, "The agent gave me a working link.")],
        replies=("Building it.", "Here is the link."),
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["entries"][1] == {
        "index": 1,
        "kind": "goal",
        "exchange_count": 1,
        "outcome": "satisfied",
        "detail": "The agent gave me a working link.",
    }
    assert context.metadata is not None
    assert context.metadata["entries"] == state["entries"]


# What one bridged curl in the recorded exec commands is, by the URL it carries. The state poll is
# the bare `/api/agents` listing, which ends the quoted inner command; the sends and the event
# fetches all address a path under it.
_CALL_KIND_BY_URL_MARKER: Final[tuple[tuple[str, str], ...]] = (
    ("/api/agents/chat-1/message", "send"),
    ("/api/agents'", "state"),
)


def _conversation_call_sequence(environment: MockBoxEnvironment) -> list[str]:
    """The workspace-state polls and client-message sends the driver made, in the order it made them."""
    return [
        kind for command in environment.exec_commands for marker, kind in _CALL_KIND_BY_URL_MARKER if marker in command
    ]


def test_driver_asks_the_client_before_the_workspace_so_a_finished_entry_costs_no_waiting_poll(
    tmp_path: Path,
) -> None:
    """Whether an entry is over is a property of the conversation the client already has. Polling
    for WAITING before asking would make every entry that ends by itself wait on an agent that may
    never report it again, and that expiry is recorded as a trial timeout."""
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal7",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?"), done(TurnOutcome.SATISFIED, "Saw it.")],
        replies=("Building it.", "Here is the link."),
    )

    sequence = _conversation_call_sequence(environment)
    # A message still goes out only after the workspace has reported WAITING.
    assert all(index > 0 and sequence[index - 1] == "state" for index, kind in enumerate(sequence) if kind == "send")
    # And the exchange that ended the entry polled nothing at all: the last poll of the run is the
    # one that collected the reply to the last message actually sent.
    assert sequence[-2:] == ["send", "state"]


def test_driver_snapshots_the_final_entry_even_when_its_client_was_satisfied_without_speaking(
    tmp_path: Path,
) -> None:
    """The `final` cadence captures the workspace the conversation built, so what matters is that
    the conversation said something -- not that its last entry did."""
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal8",
        goal="See it running",
        max_exchanges=3,
        actions=[done(TurnOutcome.SATISFIED, "The first reply already answered me.")],
        replies=("Building it; here is the link.",),
        snapshot_mode="final",
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["entries"][1]["exchange_count"] == 0
    assert state["waits_done"] == 1
    assert any("post_message_1" in command for command in environment.exec_commands)


def test_is_snapshot_wanted_takes_the_final_cadences_one_snapshot_after_the_entry_not_per_exchange() -> None:
    """Which exchange ends a goal entry is not known until its source is asked again, so a `final`
    cadence that snapshotted per exchange to keep the last would pull a tarball every time."""
    assert is_snapshot_wanted(SnapshotMode.PER_TURN, SnapshotPoint.AFTER_EXCHANGE)
    assert not is_snapshot_wanted(SnapshotMode.PER_TURN, SnapshotPoint.AFTER_FINAL_ENTRY)
    assert not is_snapshot_wanted(SnapshotMode.FINAL, SnapshotPoint.AFTER_EXCHANGE)
    assert is_snapshot_wanted(SnapshotMode.FINAL, SnapshotPoint.AFTER_FINAL_ENTRY)
    assert not is_snapshot_wanted(SnapshotMode.OFF, SnapshotPoint.AFTER_EXCHANGE)
    assert not is_snapshot_wanted(SnapshotMode.OFF, SnapshotPoint.AFTER_FINAL_ENTRY)


def test_driver_snapshots_a_multi_exchange_final_entry_once_per_exchange_under_the_per_turn_cadence(
    tmp_path: Path,
) -> None:
    _goal_source, environment, _context = _run_goal_driver(
        tmp_path,
        trial_name="todo-app__goal5",
        goal="See it running",
        max_exchanges=3,
        actions=[say("Where is it?"), say("And now?"), done(TurnOutcome.SATISFIED)],
        replies=("Building it.", "Nearly.", "Here."),
    )

    # The default cadence is per-turn, and a turn is one exchange.
    snapshot_names = sorted(
        {
            name
            for command in environment.exec_commands
            for name in ("post_message_1", "post_message_2", "post_message_3")
            if name in command
        }
    )
    assert snapshot_names == ["post_message_1", "post_message_2", "post_message_3"]


def test_goal_turn_source_sends_the_fallback_once_then_ends_the_entry() -> None:
    """With no key there is no call to make, which is the same degraded path an API failure takes."""
    source = GoalTurnSource(goal="Get a preview link", model="claude-opus-4-8", api_key=SecretStr(""))
    case_config = _case_config(("Build it", GoalEntry(goal="Get a preview link", max_exchanges=4)))
    transcript = Transcript(events=({"type": "assistant_message", "text": "Working on it."},))

    first = source.next_action(case_config, transcript)
    second = source.next_action(case_config, transcript)

    assert first == Say(text=decider.FALLBACK_MESSAGE)
    # The record says the harness degraded, not that the agent failed to satisfy the client.
    assert second == Done(reason=TurnOutcome.FALLBACK, detail=FALLBACK_ENTRY_DETAIL)
    assert [result.is_fallback for result in source.results] == [True]


def test_only_a_goal_source_reports_a_budget_as_something_that_cut_it_off() -> None:
    """A fixed-script entry has one message and is complete once it is sent; only a goal-holding
    client can actually be stopped mid-conversation."""
    assert GoalTurnSource(goal="g", model="m", api_key=SecretStr("")).exhaustion_end == Done(
        reason=TurnOutcome.BUDGET_EXHAUSTED
    )
    assert LiteralTurnSource(prompt="hi").exhaustion_end == Done(reason=TurnOutcome.COMPLETED)
    assert PersonaLLMTurnSource(model="m", api_key=SecretStr("")).exhaustion_end == Done(reason=TurnOutcome.COMPLETED)


def test_a_goal_source_whose_last_allowed_message_was_the_fallback_reports_a_fallback() -> None:
    """The fallback line is sent as a message, so on the last allowed exchange the budget stops the
    entry before the source can report FALLBACK itself. Recording that as `budget_exhausted` would
    label a harness outage as an agent that failed its client -- and at max_exchanges=1 no goal
    entry could ever be recorded as a fallback at all. The source answers with the whole ending, so
    the entry the budget pre-empted carries the same explanation as one the source reported itself."""
    source = GoalTurnSource(goal="Get a preview link", model="m", api_key=SecretStr(""))
    case_config = _case_config(("Build it", GoalEntry(goal="Get a preview link", max_exchanges=1)))

    assert source.exhaustion_end == Done(reason=TurnOutcome.BUDGET_EXHAUSTED)
    source.next_action(case_config, Transcript(events=()))

    assert source.exhaustion_end == Done(reason=TurnOutcome.FALLBACK, detail=FALLBACK_ENTRY_DETAIL)


def test_driver_explains_a_fallback_the_budget_stopped_before_the_client_could_report_it(
    tmp_path: Path,
) -> None:
    """A real `GoalTurnSource` with no key degrades exactly as an API failure does, and at
    `max_exchanges=1` the budget stops the entry on the degraded line itself. The record still has to
    say the harness failed -- that is the whole reason someone reading a bad score opens it."""
    _driver, environment, _context = _run_driver(
        tmp_path,
        (_OPENING_PROMPT, GoalEntry(goal="See it running", max_exchanges=1)),
        _goal_conversation(("Building it.", "Sure.")),
        trial_name="todo-app__goal11",
        timeout_seconds=1800.0,
        scripted_sources=[
            LiteralTurnSource(prompt=_OPENING_PROMPT),
            GoalTurnSource(goal="See it running", model="claude-opus-4-8", api_key=SecretStr("")),
        ],
    )

    state = json.loads(environment.uploaded_content_by_target["/logs/agent/state.json"])
    assert state["entries"][1] == {
        "index": 1,
        "kind": "goal",
        "exchange_count": 1,
        "outcome": "fallback",
        "detail": FALLBACK_ENTRY_DETAIL,
    }


def test_literal_turn_source_says_its_message_once_then_reports_completed() -> None:
    source = LiteralTurnSource(prompt="Build it")
    case_config = _case_config(("Build it",))
    transcript = Transcript(events=())

    assert source.next_action(case_config, transcript) == Say(text="Build it")
    assert source.next_action(case_config, transcript) == Done(reason=TurnOutcome.COMPLETED)
