"""Per-version conversational helper: answer questions about a saved version, or
apply a requested change to today's app. The agent edits files but never commits;
this engine commits any resulting change as a new PORT version.
"""

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from versioning.claude_p import ClaudeCLIError
from versioning.claude_p import claude_p_task
from versioning.data_types import AppRef
from versioning.data_types import AssistOutcome
from versioning.data_types import CommitRecord
from versioning.data_types import RestoreError
from versioning.data_types import TrailerBlock
from versioning.data_types import VersionKind
from versioning.data_types import VersionSummary
from versioning.interfaces import GitRepoInterface
from versioning.locking import operation_lock
from versioning.trailers import serialize_trailer_block

ASSIST_MODEL: Final[str] = "claude-sonnet-5"

# Read-only tool surface for browse-only apps (versioning itself, the system).
_READ_ONLY_TOOLS: Final[str] = "Read,Grep,Glob,Bash"

_MAX_MESSAGE_CHARS: Final[int] = 1000
_MAX_PRIOR_EXCHANGES: Final[int] = 6
_MAX_TITLE_CHARS: Final[int] = 80

# A callable that runs the agentic task and returns its final text; injected so
# the engine is testable without a live model. Second argument: whether the
# task may change files.
AssistTaskRunner = Callable[[str, bool], str]


class AssistError(RestoreError):
    """Raised when the helper cannot answer or apply a request."""

    ...


_SHARED_ROLE: Final[str] = (
    "For this task you are the workspace's versioning helper: you help a non-technical user "
    "understand the saved versions of their app. You may freely read the repo and use read-only git "
    "commands (log/show/diff) to inspect the version in question and compare it with the current app. "
    "Never run git commands that write (commit/add/checkout/restore/reset/clean), never restart "
    "services, and never touch files outside the app's folder. Answer in plain language for someone "
    "who has never heard of git: no shas, commits, diffs, branches, or file paths unless the user "
    "explicitly asks for technical detail. Keep answers to a few sentences. Reply with ONLY the "
    "answer text -- it is shown directly in a chat bubble."
)

_CHANGE_ABILITY: Final[str] = (
    " If -- and only if -- the user asks for a change to today's app (bring something back from this "
    "version, re-apply a look or behavior), make the change by editing files under the app's folder, "
    "adapted to how the app works now; leave all changes uncommitted. When you changed files, end your "
    "reply with one line starting with `CHANGE-NOTE: ` followed by a plain-language description of the "
    "change (under 80 characters). If the request cannot be applied cleanly, change nothing and say "
    "why in your answer. For pure questions, change nothing."
)

_READ_ONLY_LIMIT: Final[str] = (
    " You are READ-ONLY for this app: never create, modify, or delete any file. If the user asks for "
    "a change, explain that this one can be browsed but not changed from here."
)


@pure
def _build_assist_prompt(
    app: AppRef,
    commit: CommitRecord,
    summary: VersionSummary | None,
    prior_exchanges: Sequence[Mapping[str, str]],
    message: str,
) -> str:
    parts: list[str] = [
        f"The user is looking at a saved version of their app '{app.title}' "
        f"(code lives under {app.package_dir}, version commit {commit.sha}).",
        f"That version's record:\n{commit.subject}\n{commit.body}".strip(),
    ]
    if summary is not None:
        parts.append(f"Its plain-language summary: {summary.title} - {summary.description}")
    for exchange in list(prior_exchanges)[-_MAX_PRIOR_EXCHANGES:]:
        prior_question = exchange.get("question", "")
        prior_answer = exchange.get("answer", "")
        if prior_question and prior_answer:
            parts.append(f"Earlier message: {prior_question}\nEarlier answer: {prior_answer}")
    parts.append(f"The user's message: {message[:_MAX_MESSAGE_CHARS]}")
    return "\n\n".join(parts)


def run_assist_task(prompt: str, is_change_allowed: bool) -> str:
    """The live task runner. Raises AssistError if the agent call itself fails."""
    role = _SHARED_ROLE + (_CHANGE_ABILITY if is_change_allowed else _READ_ONLY_LIMIT)
    tools = None if is_change_allowed else _READ_ONLY_TOOLS
    try:
        result = claude_p_task(prompt, append_system=role, model=ASSIST_MODEL, tools=tools)
    except (ClaudeCLIError, OSError) as e:
        raise AssistError(f"The helper could not run: {e}") from e
    return result.text


@pure
def _split_answer_and_change_note(response_text: str) -> tuple[str, str | None]:
    answer_lines: list[str] = []
    change_note: str | None = None
    for line in response_text.splitlines():
        if line.strip().startswith("CHANGE-NOTE:"):
            change_note = line.split(":", 1)[1].strip()[:_MAX_TITLE_CHARS]
        else:
            answer_lines.append(line)
    return "\n".join(answer_lines).strip(), change_note


def perform_assist(
    git_repo: GitRepoInterface,
    app: AppRef,
    version_sha: str,
    commit: CommitRecord,
    summary: VersionSummary | None,
    prior_exchanges: Sequence[Mapping[str, str]],
    message: str,
    is_change_allowed: bool,
    lock_file: Path,
    task_runner: AssistTaskRunner,
) -> AssistOutcome:
    """Run one exchange with the helper; commit its change (if any) as a new version.

    Raises AssistError when the helper cannot run or the app is mid-edit.
    """
    with operation_lock(lock_file):
        dirty_before = git_repo.read_dirty_paths_under(app.package_dir)
        if len(dirty_before) > 0:
            raise AssistError("The app has unsaved edits in progress; try again in a moment")

        prompt = _build_assist_prompt(app, commit, summary, prior_exchanges, message)
        try:
            response_text = task_runner(prompt, is_change_allowed)
        except AssistError:
            # Drop any partial edits so a failed exchange never leaks into the app.
            if len(git_repo.read_dirty_paths_under(app.package_dir)) > 0:
                git_repo.restore_path_to_commit("HEAD", app.package_dir)
            raise

        answer, change_note = _split_answer_and_change_note(response_text)
        changed_paths = git_repo.read_dirty_paths_under(app.package_dir)
        if len(changed_paths) == 0:
            return AssistOutcome(answer=answer, new_version_sha=None)
        if not is_change_allowed:
            logger.warning("Helper changed {} files of browse-only app {}; reverting", len(changed_paths), app.name)
            git_repo.restore_path_to_commit("HEAD", app.package_dir)
            return AssistOutcome(answer=answer, new_version_sha=None)

        title = change_note if change_note else f"Changed {app.title} from a conversation"
        assist_trailers = serialize_trailer_block(
            TrailerBlock(
                app_name=app.name,
                request=title,
                kind=VersionKind.PORT,
                ported_from_sha=version_sha,
            )
        )
        new_sha = git_repo.commit_paths(
            [app.package_dir],
            f"versioning: change {app.name} from a conversation about {version_sha[:10]}\n\n{assist_trailers}",
        )
        logger.debug("Assist changed {} files of {}", len(changed_paths), app.name)
        return AssistOutcome(answer=answer, new_version_sha=new_sha)
