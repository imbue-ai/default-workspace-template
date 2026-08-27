import re
import subprocess
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from versioning.data_types import ChangeKind
from versioning.data_types import ChangeStats
from versioning.data_types import CommitRecord
from versioning.data_types import FileChange
from versioning.data_types import GitReadError
from versioning.interfaces import GitRepoInterface
from versioning.trailers import parse_git_log_output

_GIT_TIMEOUT_SECONDS: Final[float] = 30.0

# A binary file's churn has no line count ("-" in numstat); count it as a fixed weight.
_BINARY_FILE_LINE_WEIGHT: Final[int] = 40

_SHA_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")

# These control characters cannot appear in commit messages.
_LOG_FIELD_SEPARATOR: Final[str] = "\x1f"
_LOG_RECORD_SEPARATOR: Final[str] = "\x1e"
LOG_FORMAT: Final[str] = (
    "%H" + _LOG_FIELD_SEPARATOR + "%an" + _LOG_FIELD_SEPARATOR + "%aI" + _LOG_FIELD_SEPARATOR + "%B" + _LOG_RECORD_SEPARATOR
)


@pure
def _count_diff_stat_files(diff_name_only_output: str) -> int:
    return len([line for line in diff_name_only_output.splitlines() if line.strip()])


@pure
def parse_numstat_log(output: str) -> dict[str, ChangeStats]:
    """Parse `git log --format=%H --numstat` output; each numstat line belongs to the sha above it."""
    stats_by_sha: dict[str, ChangeStats] = {}
    current_sha: str | None = None
    files = 0
    lines = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SHA_LINE_PATTERN.match(line):
            if current_sha is not None:
                stats_by_sha[current_sha] = ChangeStats(files_changed=files, lines_changed=lines)
            current_sha = line
            files = 0
            lines = 0
            continue
        parts = line.split("\t")
        if len(parts) < 3 or current_sha is None:
            continue
        added, removed = parts[0], parts[1]
        files += 1
        if added == "-" or removed == "-":
            lines += _BINARY_FILE_LINE_WEIGHT
        else:
            lines += int(added) + int(removed)
    if current_sha is not None:
        stats_by_sha[current_sha] = ChangeStats(files_changed=files, lines_changed=lines)
    return stats_by_sha


class SubprocessGitRepo(GitRepoInterface):
    def _run_git(self, arguments: list[str]) -> str:
        command = ["git", "-C", str(self.repo_root)] + arguments
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            raise GitReadError(f"git timed out: {' '.join(command)}") from e
        if completed.returncode != 0:
            raise GitReadError(f"git failed ({completed.returncode}): {' '.join(command)}: {completed.stderr.strip()}")
        return completed.stdout

    def read_commits_touching_path(self, relative_path: str) -> list[CommitRecord]:
        # --first-parent: without it, upstream template merges flood app history with vendor churn.
        output = self._run_git(
            ["log", "--first-parent", "--reverse", f"--format={LOG_FORMAT}", "--", relative_path]
        )
        return parse_git_log_output(output, _LOG_FIELD_SEPARATOR, _LOG_RECORD_SEPARATOR)

    def read_change_stats_by_sha(self, relative_path: str) -> dict[str, ChangeStats]:
        output = self._run_git(
            ["log", "--first-parent", "--format=%H", "--numstat", "--", relative_path]
        )
        return parse_numstat_log(output)

    def read_diff_stat_between(self, sha: str, relative_path: str) -> str:
        return self._run_git(["diff", "--stat", sha, "--", relative_path]).strip()

    def read_changed_file_count_between(self, sha: str, relative_path: str) -> int:
        output = self._run_git(["diff", "--name-only", sha, "--", relative_path])
        return _count_diff_stat_files(output)

    def read_diff_of_commits(self, shas: list[str], relative_path: str) -> str:
        if len(shas) == 0:
            return ""
        # A parentless first commit diffs against git's empty-tree hash.
        first_sha = shas[0]
        last_sha = shas[-1]
        parent_output = self._run_git(["rev-list", "--parents", "-n", "1", first_sha]).split()
        base = parent_output[1] if len(parent_output) > 1 else self._run_git(["hash-object", "-t", "tree", "/dev/null"]).strip()
        return self._run_git(["diff", base, last_sha, "--", relative_path])

    def read_file_changes_of_commit(self, sha: str, relative_path: str) -> list[FileChange]:
        parent_output = self._run_git(["rev-list", "--parents", "-n", "1", sha]).split()
        base = parent_output[1] if len(parent_output) > 1 else self._run_git(["hash-object", "-t", "tree", "/dev/null"]).strip()
        output = self._run_git(["diff", "--name-status", base, sha, "--", relative_path])
        changes: list[FileChange] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0], parts[-1]
            if status.startswith("A"):
                change_kind = ChangeKind.ADDED
            elif status.startswith("D"):
                change_kind = ChangeKind.REMOVED
            else:
                change_kind = ChangeKind.EDITED
            display_path = path[len(relative_path) :].lstrip("/") if path.startswith(relative_path) else path
            changes.append(FileChange(display_path=display_path, change_kind=change_kind))
        return changes

    def read_dirty_paths_under(self, relative_path: str) -> list[str]:
        output = self._run_git(["status", "--porcelain", "--", relative_path])
        return [line[3:].strip() for line in output.splitlines() if line.strip()]

    def commit_paths(self, relative_path: str, message: str) -> str:
        self._run_git(["add", "-A", "--", relative_path])
        self._run_git(["commit", "-m", message, "--", relative_path])
        return self._run_git(["rev-parse", "HEAD"]).strip()

    def restore_path_to_commit(self, sha: str, relative_path: str) -> None:
        # rm-then-checkout so files added after the sha disappear; changes are left staged.
        logger.debug("Restoring {} to {}", relative_path, sha)
        self._run_git(["rm", "-r", "-q", "-f", "--ignore-unmatch", "--", relative_path])
        self._run_git(["checkout", sha, "--", relative_path])
        self._run_git(["clean", "-qfd", "--", relative_path])

    def read_file_at_commit(self, sha: str, repo_relative_file: str) -> str | None:
        try:
            return self._run_git(["show", f"{sha}:{repo_relative_file}"])
        except GitReadError:
            return None
