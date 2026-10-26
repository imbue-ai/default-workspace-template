"""Codex's web look-up: ``codex exec`` with web search switched on, read-only, bound to the chat's account folder."""

import tempfile
import time
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.codex.account_models import codex_account_environment
from imbue.chat.harnesses.web_lookup import WebLookup
from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.harnesses.web_lookup import WebLookupTimeoutError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# Several searches and page reads, which take longer than the one-shot completions' single answer.
_TIMEOUT_SECONDS: Final = 300.0
_SLOW_WARNING_SECONDS: Final = 90.0
_ANSWER_FILENAME: Final = "answer.txt"


@pure
def codex_web_lookup_argv(model: str | None, answer_path: Path) -> list[str]:
    """The ``codex exec`` argv, with web search on and nothing that can write.

    The prompt goes on stdin (``-``), so a message starting with ``-`` is never a flag. The account folder is a
    CODEX_HOME whose own config has no place in a search (and whose agents have web search switched off), so it
    is ignored, and its auth is still read. ``model`` None leaves the account's default.
    """
    return [
        "codex",
        "exec",
        *(["--model", model] if model else []),
        "-c",
        "tools.web_search.enabled=true",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        str(answer_path),
        "-",
    ]


class CodexWebLookup(WebLookup):
    """Runs ``codex exec`` from an empty directory with no tools that can write, and no session kept."""

    def search(self, account_dir: Path, system_prompt: str, prompt: str, model: str | None) -> str:
        # codex exec takes no system prompt of its own, so the instructions lead the prompt.
        full_prompt = f"{system_prompt}\n\n{prompt}"
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_web_lookup_") as isolated_dir:
            answer_path = Path(isolated_dir) / _ANSWER_FILENAME
            try:
                finished = run_local_command_modern_version(
                    command=codex_web_lookup_argv(model, answer_path),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=codex_account_environment(account_dir),
                    name="codex web look-up",
                    stdin_bytes=full_prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise WebLookupError(f"codex exec could not run: {e}") from e
            if finished.is_timed_out:
                raise WebLookupTimeoutError(f"codex exec did not answer within {_TIMEOUT_SECONDS:.0f}s")
            if finished.returncode != 0:
                raise WebLookupError(f"codex exec exited {finished.returncode}: {finished.stderr.strip()[-300:]}")
            answer = answer_path.read_text(encoding="utf-8").strip() if answer_path.is_file() else ""
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("codex web look-up took {:.1f}s", elapsed_seconds)
        if not answer:
            raise WebLookupError("codex exec gave no answer")
        return answer
