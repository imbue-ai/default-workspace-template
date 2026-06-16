"""Window error watcher service.

Scans every tmux window in the session for output matching /error|exception/i
and, on newly-appeared matches, sends one batched message to a randomly
selected mngr agent. The polling loop and tmux/mngr I/O are wired up in main();
the functions below are the pure, side-effect-free core (matching, dedup, alert
formatting, mngr argv assembly, agent parsing, and recipient selection).
"""

import json
import random
import re
from collections.abc import Mapping, Sequence
from typing import Final, NamedTuple

from loguru import logger

# Single source of truth for the match (REQ-MATCH-1, REQ-MATCH-2, REQ-MATCH-4).
# main() may override this at startup via the ERROR_WATCHER_PATTERN env var, so
# the pattern is threaded into match_lines() rather than read globally.
DEFAULT_ERROR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"error|exception", re.IGNORECASE
)

# Each matching line is truncated to this length in the alert so a single giant
# traceback line cannot blow up the message sent to the agent.
MAX_ALERT_LINE_LENGTH: Final[int] = 500


class AgentSummary(NamedTuple):
    """One agent from `mngr list --format json`, reduced to the fields we need.

    `state` is the agent's lifecycle state string (e.g. RUNNING, WAITING,
    STOPPED); the messageable filter keys off it.
    """

    name: str
    state: str


def match_lines(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Return the lines of `text` that contain a match for `pattern`, in order."""
    return [line for line in text.splitlines() if pattern.search(line)]


def new_matches(
    window: str, current: Sequence[str], seen: dict[str, set[str]]
) -> list[str]:
    """Return the matching lines for `window` not already alerted on, recording them as seen.

    `seen` maps window name -> set of lines already alerted on. A line present
    in `seen[window]` is suppressed; every other line is returned (once) and
    added to `seen[window]`, so a static error on screen alerts exactly once
    (REQ-MATCH-3).
    """
    already_alerted = seen.setdefault(window, set())
    fresh_lines: list[str] = []
    for line in current:
        if line in already_alerted:
            continue
        already_alerted.add(line)
        fresh_lines.append(line)
    return fresh_lines


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_ALERT_LINE_LENGTH:
        return line
    return line[:MAX_ALERT_LINE_LENGTH] + "..."


def format_alert(session: str, matches_by_window: Mapping[str, Sequence[str]]) -> str:
    """Build one human-readable alert covering every window that newly matched this poll.

    A single message names each window and includes its matching line(s), so
    multiple windows erroring in one poll yield one batched message rather than
    one per window (REQ-NOTIFY-2, REQ-NOTIFY-6).
    """
    header = (
        f"Possible error/exception detected by error-watcher in session '{session}':"
    )
    window_lines = [
        f"- window '{window}': {' | '.join(_truncate_line(line) for line in lines)}"
        for window, lines in matches_by_window.items()
    ]
    return "\n".join([header, *window_lines])


def build_list_command() -> list[str]:
    """Build the `mngr list` argv used to enumerate agents."""
    return ["mngr", "list", "--format", "json"]


def build_message_command(agent_name: str, message: str) -> list[str]:
    """Build the `mngr message` argv used to alert one agent."""
    return ["mngr", "message", agent_name, "-m", message]


def parse_agent_summaries(stdout: str) -> list[AgentSummary]:
    """Parse `mngr list --format json` output into name/state summaries.

    The CLI emits `{"agents": [{"name": ..., "state": ..., ...}], "errors": [...]}`.
    Tolerant by design (REQ-SPAWN-4): malformed or unexpected output yields an
    empty list plus a warning so the poll loop never crashes. Agents missing a
    usable name or state are skipped, since the messageable filter needs both.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning(
            "Skipped agent enumeration: mngr list output was not valid JSON: {}", e
        )
        return []
    if not isinstance(payload, dict):
        logger.warning(
            "Skipped agent enumeration: mngr list output was not a JSON object: {!r}",
            payload,
        )
        return []
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        logger.warning(
            "Skipped agent enumeration: mngr list 'agents' field was not a list: {!r}",
            agents,
        )
        return []
    summaries: list[AgentSummary] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        name = agent.get("name")
        state = agent.get("state")
        if isinstance(name, str) and name and isinstance(state, str) and state:
            summaries.append(AgentSummary(name=name, state=state))
    return summaries


def choose_recipient(names: Sequence[str], rng: random.Random) -> str | None:
    """Return a uniformly random name, or None if `names` is empty (REQ-NOTIFY-5)."""
    if not names:
        return None
    return rng.choice(list(names))


def main() -> None:
    """Console-script entry point. The polling loop is wired up separately."""
    logger.info("Starting error watcher")
