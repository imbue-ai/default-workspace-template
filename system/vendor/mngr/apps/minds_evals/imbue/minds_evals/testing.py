import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.driver import EVAL_USER_ID_NAMESPACE

# The scheduled CI workflow. It hard-codes things this package also decides -- the Modal environment
# prefix its sweep matches on, the summary file names its report composes, and the model field names
# its `jq` reads by string key -- because a GitHub Actions workflow cannot import Python. Tests read
# it back to hold those ends together, so they all name it from here rather than each spelling the
# path again.
SCHEDULED_WORKFLOW_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / ".github" / "workflows" / "minds-evals-scheduled.yml"
)


def read_scheduled_workflow_text() -> str:
    """That workflow's text, for the tests that hold its hard-coded literals to this package's
    models. Its presence is asserted rather than assumed, so a moved or renamed workflow reads as
    itself instead of as a FileNotFoundError inside an assertion about something else."""
    assert SCHEDULED_WORKFLOW_PATH.is_file(), "expected the scheduled workflow at {}".format(SCHEDULED_WORKFLOW_PATH)
    return SCHEDULED_WORKFLOW_PATH.read_text()


# Verbatim the CI_ENVIRONMENT_PREFIX env value in that workflow. Two tests in
# cleanup_environments_test hold it in place -- one reads the workflow and compares, the other
# builds an environment name the way a CI trial does and asserts this prefix selects it -- so every
# test that needs the prefix takes it from here, and those two cover all of them.
CI_SWEEP_PREFIX: Final[str] = "minds-staging-evals-ci-"

# The MNGR_PREFIX the staging activation exports inside the box, which mngr's modal provider puts in
# front of every user id it turns into an environment name.
STAGING_MNGR_PREFIX: Final[str] = "minds-staging-"

# An environment a developer's own run left behind: the eval namespace with no CI stamp in it. Every
# sweep in the suite has to leave this one standing.
DEVELOPER_ENVIRONMENT_NAME: Final[str] = "{}{}todo-app-envr-cafe1234".format(
    STAGING_MNGR_PREFIX, EVAL_USER_ID_NAMESPACE
)


def expected_modal_environment_name(trial_name: str) -> str:
    """The Modal environment `write_trial_dir` records for a trial, for a caller asserting on it.

    Built here and used by the fixture itself, so an assertion cannot end up naming an environment
    no trial in the fixture ever recorded.
    """
    return "{}{}{}".format(STAGING_MNGR_PREFIX, EVAL_USER_ID_NAMESPACE, trial_name)


class LocalGitRepo(FrozenModel):
    """A throwaway local git repo standing in for a remote (unit tests make no network requests)."""

    repo_dir: Path = Field(description="The repo's working directory, usable as a git remote url")
    commit_shas: tuple[str, ...] = Field(description="Every commit sha on 'main', oldest first")


def commit_readme_revision(repo_dir: Path, readme_content: str, message: str) -> str:
    """Rewrite README.md, commit it on the repo's current branch, and return the new commit's sha."""
    (repo_dir / "README.md").write_text(readme_content)
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    # Identity and signing are set per invocation so the commit does not depend
    # on the developer's global git config (a global commit.gpgsign would try to
    # sign these throwaway commits and fail).
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.email=test@test",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_local_git_repo(parent_dir: Path, repo_name: str, commit_count: int) -> LocalGitRepo:
    """Build a repo on branch 'main' whose every commit rewrites README.md with its own index, so a
    checkout's content identifies which commit it is at."""
    repo_dir = parent_dir / repo_name
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    commit_shas = [
        commit_readme_revision(
            repo_dir, "{} revision {}\n".format(repo_name, commit_idx), "commit {}".format(commit_idx)
        )
        for commit_idx in range(commit_count)
    ]
    return LocalGitRepo(repo_dir=repo_dir, commit_shas=tuple(commit_shas))


def tag_commit(repo_dir: Path, tag_name: str, commit_sha: str, *, is_annotated: bool = False) -> None:
    """Point a tag at a commit. An annotated tag is a tag OBJECT with its own sha, so only that kind
    exercises the peeling a ref resolver has to do to reach the commit."""
    annotation_args = ["-a", "-m", "release {}".format(tag_name)] if is_annotated else []
    subprocess.run(
        # Signing is disabled per invocation for the same reason commits disable it: a developer's
        # global tag.gpgsign would otherwise try to sign these throwaway tags and fail.
        ["git", "-C", str(repo_dir), "-c", "user.email=test@test", "-c", "user.name=test", "-c", "tag.gpgsign=false"]
        + ["tag", *annotation_args, tag_name, commit_sha],
        check=True,
    )


def create_branch(repo_dir: Path, branch_name: str, commit_sha: str) -> None:
    """Point a new branch at a commit, leaving the checked-out branch alone."""
    subprocess.run(["git", "-C", str(repo_dir), "branch", branch_name, commit_sha], check=True)


def program_block(program: str, *registrations: tuple[str, str]) -> str:
    """One supervisord `[program:*]` block that forwards a port for each (name, url) it registers."""
    forwards = " && ".join(
        "python3 system/scripts/forward_port.py --url {} --name {}".format(url, name) for name, url in registrations
    )
    return '[program:{}]\ncommand=bash -c "{}"\n\n'.format(program, forwards)


# The workspace's own system/supervisord.conf, which before the first turn is still the pinned
# template's file verbatim. Only an app whose forward_port.py call sits in the config is visible
# through it.
TEMPLATE_SUPERVISORD_CONF: Final[str] = "".join(
    (
        program_block("system_interface", ("system_interface", "http://localhost:8000")),
        "[program:chat]\ncommand=chat-app\n\n",
        "[program:terminal]\ncommand=terminal-app\n\n",
        program_block("browser", ("browser", "http://localhost:8200")),
        "[program:files]\ncommand=files-app\n\n",
        "[program:owner-exec]\ncommand=bash system/services/owner_exec/run.sh\n\n",
    )
)
TEMPLATE_CONFIG_REGISTRATIONS: Final[frozenset[str]] = frozenset({"system_interface", "browser"})
# The template apps that register from inside the program they run, which only the registry half
# sees; the registry also marks `owner-exec` `internal`.
SELF_REGISTERED_APPS: Final[frozenset[str]] = frozenset({"terminal", "files", "chat", "owner-exec"})
TEMPLATE_PREEXISTING_APPS: Final[frozenset[str]] = TEMPLATE_CONFIG_REGISTRATIONS | SELF_REGISTERED_APPS

# A workspace agent id in the shape the forward proxy routes on (`agent-<32 hex>`). Mixed digits
# rather than one repeated character, so a wrong slice of it can never accidentally match.
FAKE_WORKSPACE_AGENT_ID: Final[str] = "agent-" + "0123456789abcdef" * 2


def probe_sections(**named_bodies: str) -> str:
    """What a multi-section box probe prints: each body under its section marker, in order."""
    return "".join(
        "{}\n{}".format(evidence_collection.section_marker(name), body) for name, body in named_bodies.items()
    )


def workspace_state_output(
    registry: str,
    *,
    registry_status: str = evidence_collection.STATUS_PRESENT,
    services: str = "",
    supervisord: str = "",
    isolated_instances: str = "",
) -> str:
    """What one `workspace_state_command` run prints, as both the driver's pre-turn-1 snapshot and
    the evidence collector read it."""
    return probe_sections(
        repo_root="/home/user/workspace\n",
        registry_status=registry_status + "\n",
        registry=registry,
        services=services,
        supervisord=supervisord,
        isolated_instances=isolated_instances,
    )


# The structural gate criteria the verifier scores on every trial (tests/verifier/gates/checks.py).
GATES_CRITERION_NAMES: Final[tuple[str, ...]] = (
    "transcript_has_agent_reply",
    "agent_engaged_substantively",
    "all_turns_completed",
    "not_timed_out",
)


# The pinned pair every written trial says it ran on, both at the top level of its state file and
# inside its arm block, so a test can assert the two agree.
TRIAL_MNGR_SHA: Final[str] = "a" * 40
TRIAL_DWT_SHA: Final[str] = "c" * 40


def _exception_info(exception_type: str) -> dict[str, Any]:
    return {
        "exception_type": exception_type,
        "exception_message": "the box never came up",
        "exception_traceback": "",
        "occurred_at": "2026-09-01T12:00:00+00:00",
    }


def _write_harbor_trial_result(
    trial_dir: Path, job_dir: Path, case_id: str, exception_type: str, step_exception_type: str
) -> None:
    """harbor's own record of the trial. An exception at either level replaces the verifier result,
    because a trial harbor could not run is never graded."""
    trial_result: dict[str, Any] = {
        "task_name": "minds-evals/{}".format(case_id),
        "trial_name": trial_dir.name,
        "trial_uri": trial_dir.as_uri(),
        "task_id": {"path": "/tmp/minds-evals/datasets/small/{}".format(case_id)},
        "task_checksum": "0" * 40,
        "config": {
            "task": {"path": "/tmp/minds-evals/datasets/small/{}".format(case_id)},
            "trial_name": trial_dir.name,
            "trials_dir": str(job_dir),
        },
        "agent_info": {"name": "minds-persona-driver", "version": "0.1.0"},
        "verifier_result": {"rewards": {"gates": 1.0, "quality": 0.75, "reward": 0.75}},
    }
    if exception_type:
        trial_result["exception_info"] = _exception_info(exception_type)
        trial_result["verifier_result"] = None
    if step_exception_type:
        # harbor records a per-step failure on the step alone, leaving the trial-level
        # exception_info unset -- which is why the run gate has to read both.
        trial_result["step_results"] = [{"step_name": "agent", "exception_info": _exception_info(step_exception_type)}]
        trial_result["verifier_result"] = None
    (trial_dir / "result.json").write_text(json.dumps(trial_result, indent=2))


def _write_agent_state(
    trial_dir: Path,
    case_id: str,
    test_state: str,
    is_environment_recorded: bool,
    harness_config: Mapping[str, Any] | None,
) -> None:
    """The driver's own progress record, synced out of the box."""
    state: dict[str, Any] = {
        "eval_name": trial_dir.name,
        "case_name": case_id,
        "mngr_sha": TRIAL_MNGR_SHA,
        "dwt_sha": TRIAL_DWT_SHA,
        "test_state": test_state,
        "timed_out": test_state == "timed_out",
    }
    # Absent rather than empty for a trial that recorded no arm, which is the shape every state file
    # written before arms existed has. The block repeats the pinned pair the way the driver writes
    # it, so it describes a whole treatment on its own.
    if harness_config is not None:
        state["arm"] = {
            "mngr_sha": TRIAL_MNGR_SHA,
            "dwt_sha": TRIAL_DWT_SHA,
            "harness_config": dict(harness_config),
        }
    # An oracle trial writes a state but reaches no workspace, so the key is absent rather than
    # empty -- the driver only ever adds it once it has named the environment it will create.
    if is_environment_recorded:
        state["modal_environment_name"] = expected_modal_environment_name(trial_dir.name)
    (trial_dir / "agent" / "state.json").write_text(json.dumps(state))


def _write_reward_details(
    trial_dir: Path, test_state: str, failed_gate_names: tuple[str, ...], judge_raw_score: float
) -> None:
    """rewardkit's per-criterion breakdown, in both shapes it emits: one dict for a dimension that
    yielded a single reward, and a list for one that yielded a judge alongside programmatic guards."""
    (trial_dir / "verifier" / "reward-details.json").write_text(
        json.dumps(
            {
                "gates": {
                    "kind": "programmatic",
                    "criteria": [
                        {"name": name, "value": 0.0 if name in failed_gate_names else 1.0}
                        for name in GATES_CRITERION_NAMES
                    ],
                },
                "quality": [
                    {"kind": "programmatic", "criteria": [{"name": "wordiness", "value": 1.0, "raw": True}]},
                    {
                        "kind": "llm",
                        "criteria": [
                            {"name": "conciseness", "value": (judge_raw_score - 1) / 9, "raw": judge_raw_score}
                        ],
                    },
                ],
                "timed_out": test_state == "timed_out",
            }
        )
    )


def _write_evidence_manifest(
    trial_dir: Path, case_id: str, errored_entry_ids: tuple[str, ...], failed_entry_ids: tuple[str, ...]
) -> None:
    """The collector's record of what it measured. A `failed` entry is the workspace falling short
    and a `error` entry is the harness failing to find out; only the second one the run gate charges
    for, so both statuses belong in the fixtures that pin that split."""
    (trial_dir / "agent" / "verification" / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": case_id,
                "is_evidence_complete": not errored_entry_ids,
                "entries": [
                    {
                        "entry_id": "app_registered",
                        "check_class": "app",
                        "status": CheckStatus.PASSED.value,
                        "reason": "",
                    },
                    *(
                        {
                            "entry_id": entry_id,
                            "check_class": "http",
                            "status": CheckStatus.FAILED.value,
                            "reason": "probe_returned_500",
                        }
                        for entry_id in failed_entry_ids
                    ),
                    *(
                        {
                            "entry_id": entry_id,
                            "check_class": "http",
                            "status": CheckStatus.ERROR.value,
                            "reason": "probe_unavailable",
                        }
                        for entry_id in errored_entry_ids
                    ),
                ],
            }
        )
    )


def write_trial_dir(
    job_dir: Path,
    trial_name: str,
    *,
    case_id: str = "todo-app",
    test_state: str = "finished",
    failed_gate_names: tuple[str, ...] = (),
    errored_entry_ids: tuple[str, ...] = (),
    failed_entry_ids: tuple[str, ...] = (),
    judge_raw_score: float = 8.0,
    exception_type: str = "",
    step_exception_type: str = "",
    is_result_written: bool = True,
    is_state_written: bool = True,
    is_environment_recorded: bool = True,
    is_manifest_written: bool = True,
    harness_config: Mapping[str, Any] | None = None,
) -> Path:
    """One finished trial's on-disk artifacts, in the layout harbor and the driver leave behind.

    The keyword arguments reach combinations (an exception at either harbor level, a missing result
    or state file) that only ever occur when something went wrong on Modal. A trial carrying an
    exception gets no verifier output at all, because harbor never grades a trial it could not run.
    """
    trial_dir = job_dir / trial_name
    (trial_dir / "agent" / "verification").mkdir(parents=True, exist_ok=True)
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    if is_result_written:
        _write_harbor_trial_result(trial_dir, job_dir, case_id, exception_type, step_exception_type)
    if is_state_written:
        _write_agent_state(trial_dir, case_id, test_state, is_environment_recorded, harness_config)
    if not exception_type and not step_exception_type:
        _write_reward_details(trial_dir, test_state, failed_gate_names, judge_raw_score)
    if is_manifest_written:
        _write_evidence_manifest(trial_dir, case_id, errored_entry_ids, failed_entry_ids)
    return trial_dir


# A small common-transcript stream and the ATIF document mngr would build from it, in the shapes
# `mngr transcript --format jsonl` and `--format atif` write.
ATIF_STREAM_RECORDS: Final[tuple[dict[str, Any], ...]] = (
    {"type": "header", "event_id": "header-1", "emitter": "claude/common_transcript", "schema_version": "ATIF-v1.7"},
    {
        "type": "step",
        "event_id": "u1",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-09-01T00:00:00Z",
        "source": "user",
        "message": "Build it",
    },
    {
        "type": "step",
        "event_id": "a1",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-09-01T00:00:05Z",
        "source": "agent",
        "message": "Building it now.",
        "model_name": "claude-opus-4-8",
        "tool_calls": [{"tool_call_id": "call-1", "function_name": "Bash", "arguments": {"command": "ls"}}],
        "metrics": {"prompt_tokens": 1_200, "completion_tokens": 40, "cached_tokens": 1_000},
    },
    {
        "type": "observation",
        "event_id": "o1",
        "emitter": "claude/common_transcript",
        "timestamp": "2026-09-01T00:00:06Z",
        "results": [{"source_call_id": "call-1", "content": "README.md", "extra": {"tool_name": "Bash"}}],
    },
)


def atif_stream_jsonl() -> str:
    return "".join(json.dumps(record) + "\n" for record in ATIF_STREAM_RECORDS)


def atif_document() -> dict[str, Any]:
    """The workspace's built document: the stream's steps with the observation merged in, mngr's root
    enrichment, one embedded proxy subagent, and a root extra of the workspace's own."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "chat-1",
        "trajectory_id": "chat-1",
        "agent": {"name": "claude", "version": "unknown"},
        "steps": [
            {"step_id": 1, "timestamp": "2026-09-01T00:00:00Z", "source": "user", "message": "Build it"},
            {
                "step_id": 2,
                "timestamp": "2026-09-01T00:00:05Z",
                "source": "agent",
                "message": "Building it now.",
                "model_name": "claude-opus-4-8",
                "tool_calls": [{"tool_call_id": "call-1", "function_name": "Bash", "arguments": {"command": "ls"}}],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": "README.md",
                            "subagent_trajectory_ref": [
                                {"trajectory_id": "sub-1", "extra": {"subagent_kind": "mngr"}}
                            ],
                        }
                    ]
                },
                "metrics": {"prompt_tokens": 1_200, "completion_tokens": 40, "cached_tokens": 1_000},
                "extra": {"event_id": "a1", "emitter": "claude/common_transcript"},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 1_200,
            "total_completion_tokens": 40,
            "total_cached_tokens": 1_000,
            "total_steps": 2,
        },
        "extra": {"workspace_note": "kept"},
        "subagent_trajectories": [
            {
                "schema_version": "ATIF-v1.7",
                "trajectory_id": "sub-1",
                "agent": {"name": "claude", "version": "unknown"},
                "steps": [{"step_id": 1, "timestamp": "2026-09-01T00:00:05Z", "source": "user", "message": "list"}],
                "extra": {"subagent_kind": "mngr"},
            }
        ],
    }


def atif_document_json() -> str:
    return json.dumps(atif_document(), indent=2) + "\n"


def transcript_capture_output(stream_exit: str, document_exit: str, stderr: str) -> str:
    """What one `transcript_capture_command` run prints: each half's exit code and the stderr tail."""
    return probe_sections(stream_exit=stream_exit + "\n", document_exit=document_exit + "\n", stderr=stderr)


# Where the pulled transcript files land in the box, which is what `download_file` is asked for.
BOX_COMMON_TRANSCRIPT_PATH: Final[str] = "{}/{}".format(
    evidence_collection.box_verification_dir(), evidence_collection.COMMON_TRANSCRIPT_FILENAME
)
BOX_WORKSPACE_TRAJECTORY_PATH: Final[str] = "{}/{}".format(
    evidence_collection.box_verification_dir(), evidence_collection.WORKSPACE_TRAJECTORY_FILENAME
)


def captured_transcript_downloads() -> dict[str, str]:
    """The box files a healthy capture leaves for the collector to download."""
    return {
        BOX_COMMON_TRANSCRIPT_PATH: atif_stream_jsonl(),
        BOX_WORKSPACE_TRAJECTORY_PATH: atif_document_json(),
    }


# A background worker the chat agent launched through the launch-task skill, in the shapes the
# capture brings out: the launching step in the chat agent's stream and document, the worker's own
# stream and document, and the workspace's agent listing.
WORKER_NAME: Final[str] = "crystallize-todo"
WORKER_AGENT_ID: Final[str] = "agent-" + "fedcba9876543210" * 2
WORKER_LAUNCH_CALL_ID: Final[str] = "call-launch"
WORKER_TASK_FILE: Final[str] = "data/.tasks/harden/crystallize-todo/task.md"
WORKER_LAUNCH_COMMAND: Final[str] = (
    "uv run .agents/skills/launch-task/scripts/create_worker.py launch --name crystallize-todo "
    "--template worker --runtime-dir data/.tasks/harden/crystallize-todo/ --task-file " + WORKER_TASK_FILE
)
CHAT_WORK_DIR: Final[str] = "/home/user/workspace"


def worker_launch(name: str = WORKER_NAME, depth: int = 0, lead_name: str = "") -> WorkerLaunch:
    """The launch `scan_worker_launches` would have found for the worker these fixtures describe."""
    return WorkerLaunch(
        name=name, tool_call_id=WORKER_LAUNCH_CALL_ID, task_file=WORKER_TASK_FILE, depth=depth, lead_name=lead_name
    )


def worker_launch_step(step_id: int) -> dict[str, Any]:
    """The chat agent's step that launches the worker, as the workspace document carries it."""
    return {
        "step_id": step_id,
        "timestamp": "2026-09-01T00:00:10Z",
        "source": "agent",
        "message": "Handing the hardening pass to a worker.",
        "model_name": "claude-opus-4-8",
        "tool_calls": [
            {
                "tool_call_id": WORKER_LAUNCH_CALL_ID,
                "function_name": "Bash",
                "arguments": {"command": WORKER_LAUNCH_COMMAND},
            }
        ],
        "observation": {
            "results": [{"source_call_id": WORKER_LAUNCH_CALL_ID, "content": "Creating agent state... Done."}]
        },
        "metrics": {"prompt_tokens": 1_300, "completion_tokens": 20, "cached_tokens": 1_200},
    }


def atif_document_with_worker_launch() -> dict[str, Any]:
    document = atif_document()
    return {**document, "steps": [*document["steps"], worker_launch_step(3)]}


def atif_stream_jsonl_with_worker_launch() -> str:
    """The chat agent's stream with the launch as its own step and observation records."""
    launch = worker_launch_step(3)
    records = [
        *ATIF_STREAM_RECORDS,
        {
            "type": "step",
            "event_id": "a2",
            "emitter": "claude/common_transcript",
            "timestamp": launch["timestamp"],
            "source": "agent",
            "message": launch["message"],
            "model_name": launch["model_name"],
            "tool_calls": launch["tool_calls"],
            "metrics": launch["metrics"],
        },
        {
            "type": "observation",
            "event_id": "o2",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-09-01T00:00:11Z",
            "results": launch["observation"]["results"],
        },
    ]
    return "".join(json.dumps(record) + "\n" for record in records)


def worker_stream_jsonl(agent_id: str) -> str:
    """The worker's own stream: the task it was sent and the one inference that answered it."""
    records = [
        {
            "type": "header",
            "event_id": "header-" + "0" * 32,
            "emitter": "claude/common_transcript",
            "schema_version": "ATIF-v1.7",
        },
        {
            "type": "step",
            "event_id": "wu1",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-09-01T00:00:12Z",
            "source": "user",
            "message": "Harden the todo app and report back.",
        },
        {
            "type": "step",
            "event_id": "wa1",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-09-01T00:00:40Z",
            "source": "agent",
            "message": "Hardened; report pushed.",
            "model_name": "claude-opus-4-8",
            "metrics": {"prompt_tokens": 700, "completion_tokens": 60, "cached_tokens": 500},
        },
    ]
    return "".join(json.dumps(record) + "\n" for record in records)


def worker_document(agent_id: str) -> dict[str, Any]:
    """What `mngr transcript --format atif` builds for the worker stream above."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": agent_id,
        "trajectory_id": agent_id,
        "agent": {"name": "claude", "version": "unknown"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-09-01T00:00:12Z",
                "source": "user",
                "message": "Harden the todo app and report back.",
            },
            {
                "step_id": 2,
                "timestamp": "2026-09-01T00:00:40Z",
                "source": "agent",
                "message": "Hardened; report pushed.",
                "model_name": "claude-opus-4-8",
                "metrics": {"prompt_tokens": 700, "completion_tokens": 60, "cached_tokens": 500},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 700,
            "total_completion_tokens": 60,
            "total_cached_tokens": 500,
            "total_steps": 2,
        },
    }


def worker_listing_json(worker_state: str) -> str:
    """`mngr list --format json` for a workspace with the chat agent and one worker in the given state."""
    return json.dumps(
        {
            "agents": [
                {
                    "id": "chat-1",
                    "name": "EVAL-todo-app",
                    "type": "claude",
                    "state": "WAITING",
                    "work_dir": CHAT_WORK_DIR,
                },
                {
                    "id": WORKER_AGENT_ID,
                    "name": WORKER_NAME,
                    "type": "claude",
                    "state": worker_state,
                    "work_dir": "/home/user/worktrees/" + WORKER_NAME,
                },
            ]
        }
    )


def worker_listing_output(listing_json: str) -> str:
    return probe_sections(list_exit="0\n", listing=listing_json, stderr="")


def worker_capture_output(
    document_exit: str, stream_exit: str, preserved: str, report_path: str, stderr: str, *, report_exit: str = "0"
) -> str:
    """What one `worker_capture_command` run prints for a launch that named a task file: the report
    sections carry the path the task file named and, when it named one, the copy's exit status."""
    return probe_sections(
        document_exit=document_exit + "\n",
        stream_exit=stream_exit + "\n",
        preserved=preserved + ("\n" if preserved else ""),
        report_path=report_path + ("\n" if report_path else ""),
        report_exit=report_exit + "\n" if report_path else "",
        stderr=stderr,
    )


BOX_WORKERS_DIR: Final[str] = "/logs/agent/verification/workers"


def captured_worker_downloads(agent_id: str, is_document_included: bool) -> dict[str, str]:
    """The box files a healthy worker capture leaves under the workers directory."""
    worker_dir = "{}/{}".format(BOX_WORKERS_DIR, WORKER_NAME)
    downloads = {
        "{}/{}".format(BOX_WORKERS_DIR, "agents.json"): worker_listing_json("WAITING"),
        "{}/common_transcript.jsonl".format(worker_dir): worker_stream_jsonl(agent_id),
        "{}/reports/report.md".format(worker_dir): "# Report\n\nHardened.\n",
    }
    if is_document_included:
        downloads["{}/trajectory.json".format(worker_dir)] = json.dumps(worker_document(agent_id), indent=2)
    return downloads


def worker_trial_downloads(is_document_included: bool = True) -> dict[str, str]:
    """The box files of a trial whose chat agent launched the worker: its stream and document with the
    launch in them, plus the worker's own files under the workers directory."""
    return {
        BOX_COMMON_TRANSCRIPT_PATH: atif_stream_jsonl_with_worker_launch(),
        BOX_WORKSPACE_TRAJECTORY_PATH: json.dumps(atif_document_with_worker_launch()),
        **captured_worker_downloads(WORKER_AGENT_ID, is_document_included=is_document_included),
    }
