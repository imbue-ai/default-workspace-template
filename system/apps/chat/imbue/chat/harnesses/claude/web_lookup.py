"""Claude's web look-up: ``claude -p`` with only ``WebSearch`` and ``WebFetch``, under the chat's account config dir."""

import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.claude.one_shot import claude_one_shot_env
from imbue.chat.harnesses.claude.one_shot import parse_claude_print_result
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.web_lookup import WebLookup
from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.harnesses.web_lookup import WebLookupTimeoutError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# The only tools the run gets: it can search and read pages and reach nothing on this machine.
WEB_TOOLS: Final = "WebSearch,WebFetch"
# Several searches and page reads, which take longer than the one-shot completions' single answer.
_TIMEOUT_SECONDS: Final = 300.0
_SLOW_WARNING_SECONDS: Final = 90.0


@pure
def claude_web_lookup_argv(system_prompt: str, model: str | None) -> list[str]:
    """The ``claude -p`` argv: web tools only, allowed without asking, on ``model`` or the account's default.

    ``bypassPermissions`` is what lets a headless run use the web tools at all, and is safe here because
    nothing else is available to it. The prompt goes on stdin, so a message starting with ``-`` is never a flag.
    """
    return [
        "claude",
        "-p",
        "--output-format",
        "json",
        *(["--model", model] if model else []),
        "--system-prompt",
        system_prompt,
        "--tools",
        WEB_TOOLS,
        "--permission-mode",
        "bypassPermissions",
        # A persisted session would file a projects/ directory under the account for every call.
        "--no-session-persistence",
    ]


@pure
def claude_web_lookup_env(ambient_env: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """The one-shot environment (the chat's account, none of the server's own credentials), with thinking left on.

    A search that has to judge which pages are about the right person gains from it, unlike a short name.
    """
    env = claude_one_shot_env(ambient_env, config_dir)
    env.pop("MAX_THINKING_TOKENS", None)
    return env


class ClaudeWebLookup(WebLookup):
    """Runs ``claude -p`` from an empty directory, so no project instructions or hooks reach the answer."""

    def search(self, account_dir: Path, system_prompt: str, prompt: str, model: str | None) -> str:
        env = claude_web_lookup_env(os.environ, account_dir)
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chat_web_lookup_") as isolated_dir:
            try:
                finished = run_local_command_modern_version(
                    command=claude_web_lookup_argv(system_prompt, model),
                    is_checked=False,
                    timeout=_TIMEOUT_SECONDS,
                    cwd=Path(isolated_dir),
                    env=env,
                    name="claude web look-up",
                    stdin_bytes=prompt.encode("utf-8"),
                )
            except ProcessError as e:
                raise WebLookupError(f"claude -p could not run: {e}") from e
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise WebLookupTimeoutError(f"claude -p did not answer within {_TIMEOUT_SECONDS:.0f}s")
        if finished.returncode != 0:
            raise WebLookupError(f"claude -p exited {finished.returncode}: {finished.stderr.strip()[:300]}")
        if elapsed_seconds > _SLOW_WARNING_SECONDS:
            logger.warning("claude web look-up took {:.1f}s", elapsed_seconds)
        try:
            return parse_claude_print_result(finished.stdout)
        except OneShotCompletionError as e:
            raise WebLookupError(str(e)) from e
