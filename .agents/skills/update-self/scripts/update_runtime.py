"""The apply's process-level seams (``Runner``, ``HttpClient``, ``Spawner`` -- what the
tests intercept), its error types, and the git and subprocess helpers every step
shares.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class ApplyError(Exception):
    """Base class for apply failures (avoids raising built-in exceptions)."""


class ApplyPreconditionError(ApplyError):
    """A precondition was not met; nothing was changed, do not roll back."""


class ApplyFailed(ApplyError):
    """A forward apply step failed; the caller must roll the merge back.

    ``live_service_restarted`` records whether the live services agent was
    already (re)started before the failure -- recovery restarts only then, so a
    failure before the restart never blips a UI that is still serving
    known-good code. ``detail`` is captured output explaining the failure --
    the pre-flight boot's own log, or the traceback of an error nobody foresaw
    -- and ``detail_heading`` names which, since the two are read very
    differently by whoever diagnoses the rollback. stderr gets all of it, the
    rollback commit only :meth:`headline`.
    """

    def __init__(
        self,
        message: str,
        *,
        live_service_restarted: bool = False,
        detail: str = "",
        detail_heading: str = "failure output",
    ) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted
        self.detail = detail
        self.detail_heading = detail_heading

    def headline(self) -> str:
        """The message plus only the last line of ``detail`` (the payload --
        a traceback ends on the exception that names the cause)."""
        last = next(
            (
                line
                for line in reversed(self.detail.strip().splitlines())
                if line.strip()
            ),
            "",
        )
        return f"{self}: {last}" if last else str(self)


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)

    def which(self, executable: str) -> str | None:
        """Resolve ``executable`` on PATH, as the shell running us would."""
        return shutil.which(executable)

    def run_process_group(
        self, argv: Sequence[str], *, cwd: str, env: dict, timeout: float
    ) -> subprocess.CompletedProcess:
        """``run`` for a command whose own children must not outlive it.

        The command leads a new session, and a timeout kills that whole
        process group before ``TimeoutExpired`` is raised. ``subprocess.run``'s
        timeout kills only the direct child, so a hung ``bash setup_system.sh``
        would otherwise leave its curl or installer running on after the apply
        had rolled back around it. Output is captured as text, like ``run``
        with ``capture_output=True, text=True``.
        """
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass  # the group is already gone
            process.communicate()
            raise
        return subprocess.CompletedProcess(
            list(argv), process.returncode, stdout, stderr
        )


@dataclass(frozen=True)
class FetchedPage:
    """A fetched response body plus the headers the frontend probe reads."""

    status: int
    body: str
    headers: dict[str, str]

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass(frozen=True)
class FrontendProbe:
    """What the live UI said when asked whether it serves a working frontend."""

    failure: str | None
    is_answered: bool


class HttpClient:
    """Indirection over the loopback probes: the health checks (live service +
    pre-flight boot) and the frontend probe's page fetches."""

    def get_status(self, url: str, timeout: float) -> int | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None

    def get_page(self, url: str, timeout: float) -> FetchedPage | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return FetchedPage(
                    status=int(response.status), body=body, headers=headers
                )
        except urllib.error.HTTPError as exc:
            return FetchedPage(status=int(exc.code), body="", headers={})
        except (urllib.error.URLError, OSError):
            return None


@dataclass
class Spawned:
    """A handle to a spawned throwaway server process."""

    _process: subprocess.Popen
    _output_path: Path

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def has_exited(self) -> bool:
        return self._process.poll() is not None

    def read_output(self) -> str:
        try:
            return self._output_path.read_text(errors="replace")
        except OSError:
            return ""


class Spawner:
    """Indirection over ``subprocess.Popen`` for the pre-flight throwaway boot.

    The child's stdout and stderr go to ``output_path`` rather than a pipe: a
    pipe whose buffer filled would block the very boot we are timing.
    """

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> Spawned:
        with output_path.open("wb") as output_file:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=output_file,
                stderr=subprocess.STDOUT,
            )
        return Spawned(_process=process, _output_path=output_path)


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the throwaway server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def diff_name_status(
    repo_root: Path, rollback_to: str, runner: Runner
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs for ``rollback_to..HEAD``.

    ``--no-renames`` makes a rename surface as a delete + add pair, which keeps
    the rollback logic simple (restore the deletes, remove the adds).
    """
    result = runner.run(
        ["git", "diff", "--no-renames", "--name-status", rollback_to, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        pairs.append((fields[0].strip(), fields[-1].strip()))
    return pairs


def assert_clean_tree(repo_root: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise ApplyPreconditionError(
            "working tree has uncommitted changes; refusing to apply so a rollback "
            "can never clobber unrelated work. Commit or stash, then re-run."
        )


def abort_in_progress_merge(repo_root: Path, runner: Runner) -> bool:
    """Undo a ``git merge`` that was killed before it committed; report whether
    there was one.

    ``git merge`` writes ``MERGE_HEAD`` before it resolves anything and drops it
    only when the merge commit lands, so an apply killed anywhere inside its
    merge step leaves the merge *staged but uncommitted*: ``HEAD`` is still the
    rollback point, and the index holds the merged content. That state has to be
    undone before anything else commits, because git turns the next commit into
    the merge commit -- so the rollback's own commit would land the very merge it
    exists to undo, under a subject saying it was rolled back. (With conflicts
    still unresolved git refuses to commit at all, wedging every later recovery
    instead.) ``git merge --abort`` is a plain index/worktree reset back to
    ``HEAD``: no network, no package manager, exactly what the rollback is
    allowed to need.
    """
    merge_head = runner.run(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(merge_head, "returncode", 0) != 0:
        return False
    sys.stderr.write(
        "an interrupted merge is still staged (MERGE_HEAD is present); aborting it "
        "before restoring the tree.\n"
    )
    runner.run(
        ["git", "merge", "--abort"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return True


def run_checked(
    runner: Runner,
    argv: Sequence[str],
    cwd: Path,
    what: str,
    *,
    live_service_restarted: bool = False,
    env: dict | None = None,
    timeout: float | None = None,
) -> None:
    """Run an apply command; raise :class:`ApplyFailed` on a non-zero exit, when
    it cannot be spawned at all, or when it outlives its ``timeout`` budget (a
    hung step is a failure with a name, not an open-ended wait)."""
    try:
        result = runner.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ApplyFailed(
            f"{what} did not finish within {timeout:g}s (hung or stalled)",
            live_service_restarted=live_service_restarted,
        ) from None
    except OSError as exc:
        # A step that cannot be spawned at all -- npm, uv or mngr not on the
        # PATH this apply inherited, or a cwd the merged tree does not have.
        # Named here rather than left to the caller's last-resort catch, so the
        # rollback commit says which step it was.
        raise ApplyFailed(
            f"{what} could not be run ({type(exc).__name__}: {exc})",
            live_service_restarted=live_service_restarted,
        ) from exc
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise ApplyFailed(
            f"{what} failed (exit {result.returncode}): {stderr}",
            live_service_restarted=live_service_restarted,
        )


def detail_block(exc: ApplyFailed) -> str:
    return f"--- {exc.detail_heading} ---\n{exc.detail}\n" if exc.detail else ""


def tail(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    dropped = len(lines) - limit
    return "\n".join([f"[{dropped} earlier line(s) omitted]", *lines[-limit:]])


def git_out(runner: Runner, repo_root: Path, args: Sequence[str]) -> str:
    result = runner.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
