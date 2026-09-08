import json
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_evals import evidence_collection


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
        "[program:terminal]\ncommand=bash system/apps/terminal/run_ttyd.sh\n\n",
        program_block("browser", ("browser", "http://localhost:8200")),
        program_block("files", ("files", "http://localhost:8300")),
        "[program:owner-exec]\ncommand=bash system/services/owner_exec/run.sh\n\n",
    )
)
TEMPLATE_CONFIG_REGISTRATIONS: Final[frozenset[str]] = frozenset({"system_interface", "browser", "files"})
# The template apps only the registry half sees; the registry also marks `owner-exec` `internal`.
SCRIPT_REGISTERED_APPS: Final[frozenset[str]] = frozenset({"terminal", "owner-exec"})
TEMPLATE_PREEXISTING_APPS: Final[frozenset[str]] = TEMPLATE_CONFIG_REGISTRATIONS | SCRIPT_REGISTERED_APPS

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
