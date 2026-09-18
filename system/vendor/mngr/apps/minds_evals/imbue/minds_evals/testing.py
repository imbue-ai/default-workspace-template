import json
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.data_types import StepBoxFile
from imbue.minds_evals.data_types import StepPosition
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.driver import DriverEventType
from imbue.minds_evals.driver import EVAL_USER_ID_NAMESPACE
from imbue.minds_evals.expectations import expand_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.trajectory import STEP_BOUNDARY_KIND

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


# The workspace's own supervisord config as the capture prints it -- the main file followed by the
# drop-ins under `system/supervisord.conf.d/` -- which before the first turn is still the pinned
# template's own files. Only an app whose forward_port.py call sits in the config is visible
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


def file_inventory_output(entry_count: int, matched_count_by_check_id: Mapping[str, int]) -> str:
    """What one `file_inventory_command` run prints: how many entries it wrote, and each files
    check's glob match count over them."""
    return probe_sections(
        inventory_count="{}\n".format(entry_count),
        files_matched=json.dumps(dict(matched_count_by_check_id)) + "\n",
    )


def tickets_capture_output(tickets: Mapping[str, str], is_directory_present: bool = True) -> str:
    """What one `tickets_capture_command` run prints for ticket files of the given names and text."""
    return json.dumps(
        {
            "is_directory_present": is_directory_present,
            "tickets": [{"name": name, "text": text} for name, text in tickets.items()],
            "unreadable": [],
            "omitted_count": 0,
        }
    )


# Three ticket files as the vendored `tk` writes them: two closed step tickets and one open regular
# ticket, read out of a live trial's workspace snapshot.
TICKET_FILE_TEXT_BY_NAME: Final[Mapping[str, str]] = {
    "wor-8pt0.md": (
        "---\nid: wor-8pt0\nstatus: open\ndeps: []\nlinks: []\ncreated: 2026-09-14T08:04:07.893504Z\n"
        "type: chore\npriority: 2\nagent: EVAL-behaviour-v93umbb-710c8957\n---\n# DIAG regular ticket 7f3a\n\n"
    ),
    "wor-step-5umu.md": (
        "---\nclosed: 2026-09-14T08:04:39.045206Z\nstarted: 2026-09-14T08:04:35.560842Z\nid: wor-step-5umu\n"
        "status: closed\ndeps: []\nlinks: []\ncreated: 2026-09-14T08:03:36.934731Z\ntype: task\npriority: 2\n"
        "agent: EVAL-behaviour-v93umbb-710c8957\nstep: true\n---\n# DIAG beta 7f3a\n\n\n## Summary\n\nbeta done 7f3a\n"
    ),
    "wor-step-n20d.md": (
        "---\nclosed: 2026-09-14T08:04:05.943627Z\nstarted: 2026-09-14T08:03:38.490344Z\nid: wor-step-n20d\n"
        "status: closed\ndeps: []\nlinks: []\ncreated: 2026-09-14T08:02:57.381225Z\ntype: task\npriority: 2\n"
        "agent: EVAL-behaviour-v93umbb-710c8957\nstep: true\n---\n# DIAG alpha 7f3a\n\n\n## Summary\n\nalpha done 7f3a\n"
    ),
}


BEHAVIOUR_FIXTURES_DIR: Final[Path] = Path(__file__).parent / "test_fixtures"


def behaviour_trajectory_fixture(harness: str) -> dict[str, Any]:
    """The whole captured document of a live behaviour cell on one harness (`claude`, `pi-coding` or
    `codex`), which is what its last step holds."""
    path = behaviour_step_dir(harness, DIAGNOSTIC_STEP_NAMES[-1]) / "agent" / "trajectory.json"
    return json.loads(path.read_text())


def behaviour_feed_fixture(harness: str) -> list[dict[str, Any]]:
    """The workspace events the same cell's driver polled from the chat feed, which the feed serves
    cumulatively, so the last step's record carries the whole conversation."""
    path = behaviour_step_dir(harness, DIAGNOSTIC_STEP_NAMES[-1]) / "agent" / "driver_events.jsonl"
    return [
        record["event"]
        for record in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
        if record.get("type") == DriverEventType.FEED_EVENT.value
    ]


def lay_out_behaviour_workspace(
    repo_dir: Path, ticket_text_by_name: Mapping[str, str], is_report_written: bool, is_upload_placed: bool
) -> None:
    """A workspace repo as a behaviour trial leaves one: its tickets, and the worker's report and the step's
    upload when they exist."""
    tickets_dir = repo_dir / "data" / ".tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "README.md").write_text("# data/.tickets/\n\nThe tk ticket store.\n")
    for name, text in ticket_text_by_name.items():
        (tickets_dir / name).write_text(text)
    uploads_dir = repo_dir / "data" / "uploads"
    uploads_dir.mkdir(parents=True)
    (uploads_dir / "README.md").write_text("# data/uploads/\n")
    if is_report_written:
        report_path = repo_dir / "data" / ".tasks" / "launch-task" / "diag-worker-7f3a" / "reports" / "report.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("---\ntype: status\nname: done\n---\n\nok\n")
    if is_upload_placed:
        marker_path = uploads_dir / "diagupload7f3a" / "marker.txt"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text("DIAG-UPLOAD-7f3a\n")


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


DIAGNOSTIC_CASE_ID: Final[str] = "behaviour"
DIAGNOSTIC_HARNESS: Final[str] = "claude"
# The two steps a synthesized behaviour trial runs, in order.
DIAGNOSTIC_STEP_NAMES: Final[tuple[str, str]] = ("work", "check")
# What a synthesized step's client message is, which its trajectory's boundary marker names and its
# transcript facts are counted from.
DIAGNOSTIC_CLIENT_MESSAGE: Final[str] = "Carry out the items"
# What a synthesized step's spend delta and cumulative usage add up to, so a test can assert on both.
DIAGNOSTIC_INPUT_TOKEN_DELTA: Final[int] = 1_000
# The worker a synthesized step's listing, captures and trajectory all name.
DIAGNOSTIC_WORKER_NAME: Final[str] = "diag-worker-7f3a"


class DiagnosticStepFiles(FrozenModel):
    """One step of a synthesized diagnostic trial: the files under its step directory, and what
    harbor recorded about the step beside them.

    `files` is keyed by the path under `steps/<name>/`, so a test drops a key to make one record
    absent and replaces a value to make one unreadable, which is what the three fact outcomes turn on.
    """

    name: str = Field(description="The step's name; empty writes the step at the trial root, as a flat trial")
    files: dict[str, str] = Field(description="Each file's contents, by its path under the step directory")
    input_token_delta: int | None = Field(
        default=DIAGNOSTIC_INPUT_TOKEN_DELTA, description="What harbor recorded as the step's published spend"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="What harbor recorded as the step's agent metadata"
    )


def diagnostic_case_config(
    step_name: str = "",
    *,
    step_index: int = 0,
    step_total: int = 1,
    entries_before: int = 0,
    is_diagnostic_probe_run: bool = False,
    files: tuple[StepBoxFile, ...] = (),
    authored_expectations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The case config one step's instruction carries, built through the generator's own expansion so
    a test cannot state a check list the generator would never produce."""
    expectations = parse_expectations(authored_expectations, DIAGNOSTIC_CASE_ID) if authored_expectations else None
    step = (
        StepPosition(
            name=step_name,
            index=step_index,
            total=step_total,
            trial_lifetime_seconds=3600.0,
            entries_before=entries_before,
            files=files,
            is_diagnostic_probe_run=is_diagnostic_probe_run,
            seed_app=None,
        )
        if step_name
        else None
    )
    return CaseConfig(
        case_id=DIAGNOSTIC_CASE_ID,
        persona="An operator running a scripted harness self-check.",
        prompts=(DIAGNOSTIC_CLIENT_MESSAGE,),
        timeout_seconds=1800.0,
        verification_timeout_seconds=900.0,
        mngr_branch="main",
        mngr_sha=TRIAL_MNGR_SHA,
        dwt_repo=DEFAULT_DWT_REPO,
        dwt_branch="main",
        dwt_sha=TRIAL_DWT_SHA,
        expectations=expand_expectations(expectations) if expectations is not None else None,
        authored_expectations=expectations,
        step=step,
    ).model_dump(mode="json")


def diagnostic_instruction(case_config: Mapping[str, Any]) -> str:
    """One step's instruction.md: the fenced JSON block the driver's own parser reads, under enough
    prose to be the file a reader meets."""
    return "# Minds persona eval\n\nThe machine-readable case config for this step:\n\n```json\n{}\n```\n".format(
        json.dumps(case_config, indent=2)
    )


def diagnostic_state(
    step_name: str = "",
    *,
    harness: str = DIAGNOSTIC_HARNESS,
    preparation_stage: str = "conversation",
    entry_count: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    """The driver's state as a step leaves it, with every key the facts read."""
    return {
        "eval_name": "diagnostics",
        "case_name": DIAGNOSTIC_CASE_ID,
        "step_name": step_name,
        "mngr_sha": TRIAL_MNGR_SHA,
        "dwt_sha": TRIAL_DWT_SHA,
        "test_state": "finished",
        "timed_out": False,
        "timed_out_reason": "",
        "preparation_stage": preparation_stage,
        "entries": [
            {"index": index, "kind": "literal", "exchange_count": 1, "outcome": "completed", "detail": ""}
            for index in range(entry_count)
        ],
        "client_messages": [DIAGNOSTIC_CLIENT_MESSAGE] * entry_count,
        "decider_call_count": 0,
        "snapshot_byte_count": None,
        "arm": {
            "mngr_sha": TRIAL_MNGR_SHA,
            "dwt_sha": TRIAL_DWT_SHA,
            "harness_config": {"harness": harness, "model_choice_switch": "applied", "is_model_confirmed": True},
        },
        **overrides,
    }


def diagnostic_trajectory(step_names: Sequence[str] = (), *, user_step_count: int = 1) -> dict[str, Any]:
    """The published ATIF document, with one boundary marker per step run so far, each followed by
    the client message that opened its step."""
    steps: list[dict[str, Any]] = []
    for step_name in step_names:
        steps.append(
            {
                "step_id": len(steps) + 1,
                "timestamp": "2026-09-01T00:00:00Z",
                "source": "system",
                "message": "step {}".format(step_name),
                "extra": {
                    "minds_evals": {
                        "kind": STEP_BOUNDARY_KIND,
                        "step_name": step_name,
                        "opening_message": DIAGNOSTIC_CLIENT_MESSAGE,
                    }
                },
            }
        )
        steps.append(
            {
                "step_id": len(steps) + 1,
                "timestamp": "2026-09-01T00:00:01Z",
                "source": "user",
                "message": DIAGNOSTIC_CLIENT_MESSAGE,
            }
        )
        steps.append(
            {
                "step_id": len(steps) + 1,
                "timestamp": "2026-09-01T00:00:02Z",
                "source": "agent",
                "message": "Done.",
                "model_name": "claude-haiku-4-5",
                "tool_calls": [{"tool_call_id": "call-{}".format(step_name), "function_name": "Bash"}],
                "observation": {"results": [{"source_call_id": "call-{}".format(step_name), "content": "ok"}]},
            }
        )
    if not step_names:
        steps = [
            {
                "step_id": 1,
                "timestamp": "2026-09-01T00:00:01Z",
                "source": "user",
                "message": DIAGNOSTIC_CLIENT_MESSAGE,
            },
            {"step_id": 2, "timestamp": "2026-09-01T00:00:02Z", "source": "agent", "message": "Done."},
        ] * user_step_count
    return {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": "chat-1",
        "agent": {"name": "claude", "version": "unknown"},
        "steps": steps,
        "extra": {"minds_evals": {"source": "workspace"}},
        "subagent_trajectories": [],
    }


def diagnostic_reward_details(
    *, harness: str = DIAGNOSTIC_HARNESS, failed_gate_names: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "gates": {
            "kind": "programmatic",
            "criteria": [
                {"name": name, "value": 0.0 if name in failed_gate_names else 1.0} for name in GATES_CRITERION_NAMES
            ],
        },
        "harness": {"name": harness, "is_harness_quality_scored": harness == "claude"},
    }


def diagnostic_manifest(*, entries: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": DIAGNOSTIC_CASE_ID,
        "base_sha": "0" * 40,
        "dwt_tip_sha": "1" * 40,
        "preexisting_registrations": ["terminal"],
        "is_registry_present": True,
        "seeded_registrations": [],
        "is_expectations_declared": bool(entries),
        "is_evidence_complete": True,
        "started_at": "2026-09-01T00:00:00+00:00",
        "phases": [],
        "entries": list(entries),
    }


def diagnostic_step_files(
    step_name: str = "",
    *,
    step_index: int = 0,
    step_total: int = 1,
    harness: str = DIAGNOSTIC_HARNESS,
    authored_expectations: Mapping[str, Any] | None = None,
    listed_worker_names: Sequence[str] = (),
) -> dict[str, str]:
    """Every file a healthy step leaves behind, keyed by its path under the step directory.

    A test that wants a record missing pops its key, one that wants it unreadable replaces its value,
    and one that wants a different reading rewrites it -- which is how the omitted, not-recorded and
    null outcomes are reached without a driver.
    """
    entry_count = step_index + 1
    case_config = diagnostic_case_config(
        step_name,
        step_index=step_index,
        step_total=step_total,
        entries_before=step_index,
        authored_expectations=authored_expectations,
    )
    return {
        "agent/state.json": json.dumps(
            diagnostic_state(step_name, harness=harness, entry_count=entry_count), indent=2
        ),
        "agent/instruction.md": diagnostic_instruction(case_config),
        "agent/trajectory.json": json.dumps(
            diagnostic_trajectory(DIAGNOSTIC_STEP_NAMES[: step_index + 1] if step_name else ()), indent=2
        ),
        # Two lines a few minutes apart, as loguru writes them: a step's log spans the step, so a log
        # that began before the previous step ended is one that was never emptied between them.
        "agent/driver.log": (
            "2026-09-01 00:{:02d}:00.000 | INFO     | driver:1 - step {} begins\n"
            "2026-09-01 00:{:02d}:00.000 | INFO     | driver:1 - step {} ends\n"
        ).format(step_index * 10, step_name or "flat", step_index * 10 + 5, step_name or "flat"),
        "agent/driver_events.jsonl": json.dumps({"type": "feed_event", "id": "event-1"}) + "\n",
        "agent/usage.json": json.dumps({"workspace_agent": {"message_count": 1}, "decider": {"call_count": 0}}),
        "agent/verification/manifest.json": json.dumps(diagnostic_manifest(), indent=2),
        "agent/verification/apps.toml": '[[apps]]\nname = "terminal"\nurl = "http://localhost:7000/"\n',
        "agent/verification/services.txt": "terminal                         RUNNING   pid 1, uptime 0:01:00\n",
        "agent/verification/supervisord.conf": "[program:terminal]\ncommand = forward_port.py --name terminal\n",
        "agent/verification/isolated_instance_services.txt": "",
        "agent/verification/tickets.jsonl": "",
        "agent/verification/workers/agents.json": json.dumps(
            {
                "agents": [
                    {
                        "id": "agent-{}".format(name),
                        "name": name,
                        "type": harness,
                        "state": "running",
                        "work_dir": "/home/user/workspace",
                        "labels": {"agent_created": "true"},
                    }
                    for name in listed_worker_names
                ],
                "errors": [],
            }
        ),
        "agent/verification/workers/listing.json": json.dumps({"exit_code": 0, "errors": [], "is_complete": True}),
        "agent/verification/workers/captures.json": json.dumps(
            [
                {
                    "name": name,
                    "agent_id": "agent-{}".format(name),
                    "agent_type": harness,
                    "state": "running",
                    "depth": 0,
                    "lead_name": "",
                    "directory": "workers/{}".format(name),
                    "is_document_captured": True,
                    "is_stream_captured": True,
                    "is_report_captured": True,
                    "is_overflow": False,
                }
                for name in listed_worker_names
            ]
        ),
        "verifier/reward-details.json": json.dumps(diagnostic_reward_details(harness=harness), indent=2),
        "verifier/derived/judge_flows_digest.txt": "# UI flow evidence\n",
        "verifier/derived/judge_screenshots.txt": "",
    }


def diagnostic_step(
    step_name: str = "",
    *,
    step_index: int = 0,
    step_total: int = 1,
    harness: str = DIAGNOSTIC_HARNESS,
    authored_expectations: Mapping[str, Any] | None = None,
    listed_worker_names: Sequence[str] = (),
) -> DiagnosticStepFiles:
    """A healthy step, ready to be edited into whatever a test needs."""
    return DiagnosticStepFiles(
        name=step_name,
        files=diagnostic_step_files(
            step_name,
            step_index=step_index,
            step_total=step_total,
            harness=harness,
            authored_expectations=authored_expectations,
            listed_worker_names=listed_worker_names,
        ),
        metadata=diagnostic_step_metadata(cumulative_input_tokens=DIAGNOSTIC_INPUT_TOKEN_DELTA * (step_index + 1)),
    )


def diagnostic_step_metadata(
    *,
    message_count: int = 4,
    is_cost_complete: bool = True,
    worker_launch_count: int = 0,
    worker_captured_count: int = 0,
    cumulative_input_tokens: int | None = None,
) -> dict[str, Any]:
    """What harbor records as one step's agent metadata: the transcript account the usage facts read,
    and the running workspace total the spend deltas have to add up to."""
    return {
        "transcript_usage": {
            "message_count": message_count,
            "is_cost_complete": is_cost_complete,
            "worker_launch_count": worker_launch_count,
            "worker_captured_count": worker_captured_count,
        },
        "workspace_usage": {
            "tokens": {
                "input": DIAGNOSTIC_INPUT_TOKEN_DELTA if cumulative_input_tokens is None else cumulative_input_tokens,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        },
    }


def edited_diagnostic_step(step: DiagnosticStepFiles, files: Mapping[str, str | None]) -> DiagnosticStepFiles:
    """The step with each named file replaced, or dropped where its value is None.

    Dropping, corrupting and rewriting one record is how a test reaches each of the three outcomes a
    fact reports, so it is the one edit every diagnostic test makes.
    """
    edited_files = {**step.files}
    for path, contents in files.items():
        if contents is None:
            edited_files.pop(path)
        else:
            edited_files[path] = contents
    return step.model_copy_update(to_update(step.field_ref().files, edited_files))


def write_workspace_snapshot(snapshot_path: Path, tree_dir: Path) -> int:
    """A workspace snapshot in the shape `minds_bridge` writes one: the home tree tarred with
    `tar -C <home> .`, so every member carries the `./` prefix a reader has to see past."""
    home_dir = tree_dir / "home"
    (home_dir / "workspace" / "system").mkdir(parents=True, exist_ok=True)
    (home_dir / "workspace" / "system" / "index.html").write_text("<title>Todo</title>")
    with tarfile.open(snapshot_path, "w:gz") as archive:
        archive.add(home_dir, arcname=".")
    return snapshot_path.stat().st_size


def write_diagnostic_trial_dir(
    job_dir: Path,
    trial_name: str,
    steps: Sequence[DiagnosticStepFiles],
    *,
    exception_type: str = "",
) -> Path:
    """One trial of a self-diagnostic job, written in the layout harbor and the driver leave behind.

    A trial carrying an exception has no step at all: the shape a trial whose environment never
    started leaves behind.
    """
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)
    _write_harbor_trial_result(trial_dir, job_dir, DIAGNOSTIC_CASE_ID, exception_type, "")
    if exception_type:
        return trial_dir
    result_path = trial_dir / "result.json"
    trial_result = json.loads(result_path.read_text())
    is_stepped = any(step.name for step in steps)
    step_results = [
        {
            "step_name": step.name,
            "agent_result": {"n_input_tokens": step.input_token_delta, "metadata": step.metadata},
        }
        for step in steps
    ]
    if is_stepped:
        trial_result["step_results"] = step_results
    else:
        trial_result["agent_result"] = step_results[0]["agent_result"] if step_results else None
    result_path.write_text(json.dumps(trial_result, indent=2))
    for step in steps:
        step_dir = trial_dir / "steps" / step.name if step.name else trial_dir
        for relative_path, contents in step.files.items():
            path = step_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
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


def worker_listing_output(listing_json: str, *, list_exit: str = "0") -> str:
    return probe_sections(list_exit=list_exit + "\n", listing=listing_json, stderr="")


def worker_capture_output(
    document_exit: str, stream_exit: str, report_path: str, stderr: str, *, report_exit: str = "0"
) -> str:
    """What one `worker_capture_command` run prints for a launch that named a task file: the report
    sections carry the path the task file named and, when it named one, the copy's exit status."""
    return probe_sections(
        document_exit=document_exit + "\n",
        stream_exit=stream_exit + "\n",
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


# A live codex trial's captured document (codex 0.147.0 in code mode), trimmed to the steps that
# exercise how codex reaches its shell: `tk create --step` declarations, a command built in a template
# literal inside a loop, a program that failed, a worker launch whose output came back through `wait`,
# and a later `wait` that failed. Kept as JSON rather than as Python so its programs keep the `await`
# a real one carries.
CODEX_CODE_MODE_TRAJECTORY_PATH: Final[Path] = (
    Path(__file__).parent / "test_fixtures" / "codex_code_mode_trajectory.json"
)


def codex_code_mode_trajectory_document() -> dict[str, Any]:
    """The trimmed live codex document at CODEX_CODE_MODE_TRAJECTORY_PATH."""
    return json.loads(CODEX_CODE_MODE_TRAJECTORY_PATH.read_text())


# A codex code-mode program that reads two skill files, one of them twice, the way a codex agent
# invokes a skill. Kept as JavaScript rather than as Python so it keeps the `await` a real program
# carries.
CODEX_SKILL_READING_PROGRAM_PATH: Final[Path] = (
    Path(__file__).parent / "test_fixtures" / "codex_skill_reading_program.js"
)


def codex_skill_reading_program() -> str:
    """The code-mode program at CODEX_SKILL_READING_PROGRAM_PATH, exactly as a codex `_raw` argument carries it."""
    return CODEX_SKILL_READING_PROGRAM_PATH.read_text()


# --- one live behaviour cell per harness, as its trimmed job directory holds it ---

# The harnesses the behaviour family runs a cell on, as `configs/diagnostics/behaviour_harness_configs.json`
# names them and as a checked-in job directory exists for each.
BEHAVIOUR_HARNESSES: Final[tuple[str, str, str]] = ("claude", "pi-coding", "codex")

# Where each cell's own `--ak` kwargs and each family's expected-facts table are checked in.
DIAGNOSTICS_CONFIGS_DIR: Final[Path] = Path(__file__).parents[2] / "configs" / "diagnostics"
BEHAVIOUR_EXPECTED_FACTS_PATH: Final[Path] = DIAGNOSTICS_CONFIGS_DIR / "behaviour_expected_facts.json"


def behaviour_job_dir(harness: str) -> Path:
    """One live behaviour cell's job directory, trimmed to the records its facts read.

    The cell and its table are one measurement: the table says what a cell of that harness records,
    and this is the cell that did. Everything here is the trial's own record, so a fact that reads
    the wrong file, a table entry nobody measured and a reader that disagrees with what a real
    harness produced all fail on the branch rather than on the night.

    Trimmed means the snapshots, the workers' captured streams and everything else no fact opens are
    gone, and each step's driver log holds its first and last line, which are the only two the
    step-local fact reads.
    """
    return BEHAVIOUR_FIXTURES_DIR / "diagnostics_behaviour_jobs" / harness.replace("-", "_")


def behaviour_trial_dir(job_dir: Path) -> Path:
    """The one trial of a behaviour job directory."""
    (trial_dir,) = sorted(path for path in job_dir.iterdir() if path.is_dir())
    return trial_dir


def behaviour_step_dir(harness: str, step_name: str) -> Path:
    """One step of the checked-in cell, in the stepped layout harbor writes."""
    return behaviour_trial_dir(behaviour_job_dir(harness)) / "steps" / step_name


def copied_behaviour_job_dir(destination: Path, harness: str, files: Mapping[str, str | None]) -> Path:
    """A copy of that job directory with each named file -- a path under the trial directory --
    rewritten, or dropped where its value is None.

    Dropping, corrupting and rewriting one record is how a test reaches each of the three outcomes a
    fact reports without running a trial.
    """
    job_dir = destination / behaviour_job_dir(harness).name
    shutil.copytree(behaviour_job_dir(harness), job_dir)
    trial_dir = behaviour_trial_dir(job_dir)
    for relative_path, contents in files.items():
        path = trial_dir / relative_path
        assert path.is_file(), "no {} to edit in the {} cell".format(relative_path, harness)
        if contents is None:
            path.unlink()
        else:
            path.write_text(contents)
    return job_dir
