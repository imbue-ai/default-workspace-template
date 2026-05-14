"""Detect whether the chat agent already received `/welcome` and resend if not.

Invoked from the auth-success chokepoint in `claude_auth_endpoints` so a
mind whose initial `/welcome` failed for lack of credentials gets the
greeting once auth recovers.

The welcome skill's opening message text is read at runtime from
`.agents/skills/welcome/SKILL.md`, so this helper and the skill stay in
sync without manual edits.

Side-effecting dependencies (tmux pane capture and agent message
dispatch) are exposed as module-level callables so tests rebind them
rather than relying on `unittest.mock`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from loguru import logger as _loguru_logger

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.minds_workspace_server.agent_discovery import send_message

logger = _loguru_logger

_DEFAULT_SKILL_PATH = Path(".agents/skills/welcome/SKILL.md")
_FRONTMATTER_DELIMITER = "---"
_HEADER_LINE_REGEX = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_WELCOME_COMMAND = "/welcome"
_TMUX_CAPTURE_TIMEOUT_SECONDS = 5.0


class WelcomeResendError(RuntimeError):
    """Raised when the welcome skill cannot be parsed for its opening line."""


PaneCaptureFn = Callable[[str], "str | None"]
MessageSendFn = Callable[[str, str], bool]


def _strip_frontmatter(body: str) -> str:
    """Drop YAML frontmatter (between leading `---` lines) from a markdown doc."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return body
    for end_index in range(1, len(lines)):
        if lines[end_index].strip() == _FRONTMATTER_DELIMITER:
            return "\n".join(lines[end_index + 1 :])
    return body


def _extract_first_message_header(skill_body: str) -> str | None:
    """Return the first markdown header that appears inside a verbatim block.

    The welcome skill wraps its message in a pair of `---` separators. The
    actual greeting starts with a `###` header on the first non-empty
    line of that block. Walking through every header in the document and
    taking the first one that appears after a `---` separator handles
    that layout without hard-coding which skill format we're parsing.
    """
    inside_block = False
    for line in skill_body.splitlines():
        stripped = line.strip()
        if stripped == _FRONTMATTER_DELIMITER:
            inside_block = not inside_block
            continue
        if inside_block and _HEADER_LINE_REGEX.match(line):
            return line.strip()
    return None


def read_welcome_opening_line(skill_path: Path | None = None) -> str:
    """Read the welcome skill markdown and return the opening line of the message.

    Falls back to scanning the whole body if no separator-wrapped verbatim
    block is present, in case the skill layout changes in a future
    revision.
    """
    path = skill_path or _DEFAULT_SKILL_PATH
    text = path.read_text()
    body = _strip_frontmatter(text)
    header = _extract_first_message_header(body)
    if header is not None:
        return header
    match = _HEADER_LINE_REGEX.search(body)
    if match is not None:
        return match.group(0).strip()
    raise WelcomeResendError(f"Could not find a verbatim opening line in welcome skill at {path}")


def _default_capture_agent_pane(agent_name: str) -> str | None:
    """Return the tmux pane content for `agent_name`, or None if capture failed."""
    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_name}"
    command = ["tmux", "capture-pane", "-t", session_name, "-S", "-", "-p"]
    try:
        result = run_local_command_modern_version(
            command=command,
            cwd=None,
            is_checked=False,
            timeout=_TMUX_CAPTURE_TIMEOUT_SECONDS,
        )
    except ProcessSetupError as e:
        logger.warning("tmux capture-pane process setup failed for {}: {}", session_name, e)
        return None
    if result.returncode != 0:
        logger.warning(
            "tmux capture-pane failed for {}: {}",
            session_name,
            result.stderr.strip(),
        )
        return None
    return result.stdout


# Injectable module-level dependencies. Production code uses the defaults
# below; tests rebind these directly (welcome_resend.capture_pane = fake)
# instead of using `unittest.mock`.
capture_pane: PaneCaptureFn = _default_capture_agent_pane
send_message_fn: MessageSendFn = send_message


def _pane_contains_welcome(pane: str | None, opening_line: str) -> bool:
    """Treat a missing/empty pane as 'welcome absent' so we resend.

    Per the welcome-resend-race open question, a fresh mind whose agent
    has not yet printed anything is fine to re-welcome — the worst case
    is the user sees the greeting twice.
    """
    if not pane:
        return False
    return opening_line in pane


def check_and_resend_welcome(agent_name: str, skill_path: Path | None = None) -> bool:
    """If the agent's pane lacks the welcome opening line, dispatch `/welcome`.

    Returns True when a resend was issued, False when the pane already had
    the welcome (no-op).
    """
    try:
        opening_line = read_welcome_opening_line(skill_path)
    except (OSError, WelcomeResendError) as e:
        logger.warning("Could not read welcome skill opening line: {}", e)
        return False

    pane = capture_pane(agent_name)
    if _pane_contains_welcome(pane, opening_line):
        logger.debug("Agent {} pane already shows welcome; skipping resend", agent_name)
        return False

    logger.info("Resending /welcome to agent {} (pane missing opening line)", agent_name)
    sent = send_message_fn(agent_name, _WELCOME_COMMAND)
    if not sent:
        logger.warning("Failed to dispatch /welcome to agent {}", agent_name)
        return False
    return True
