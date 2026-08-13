#!/usr/bin/env python3
"""Atlas Summary -- a plain-English "what changed" recap in chat after a big task.

A non-technical user who ran a long task wants to know what changed without
having to ask "what did you do?". This detects that a large task just ended and
returns a one-time nudge; the Stop hook feeds that nudge back to the working
agent (exit 2), which then ends its reply with a short, outcomes-focused summary
written from its own context. The agent is the only thing that can post to chat,
so it -- not a background worker -- writes the summary (design "Option A").

Detection is deliberately independent of the one-pager book: it fires for *every*
large task, whether or not the work is tracked as an Atlas topic. "Large" is
measured on the just-ended task alone -- `atlas_checkpoint.LARGE_TASK_TURNS`
assistant turns since the last user message, from the current agent's own
transcript (no topic scoping) -- so a small task never triggers a summary just
because earlier small tasks accumulated.

Gated by the `summary` toggle (`atlas_config`), so a user can run the book, the
in-chat summary, or both.

Usage:
    atlas_summary.py --check [--repo-root R] [--now EPOCH]
        # prints the nudge text (and advances the baseline) iff a summary is due;
        # otherwise prints nothing. Always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_checkpoint  # noqa: E402
import atlas_common  # noqa: E402
import atlas_config  # noqa: E402
import atlas_transcript  # noqa: E402

# Don't fire two summaries within this window (belt-and-suspenders against a
# re-engaged agent stopping again immediately; the per-task guard already covers it).
SUMMARY_MIN_INTERVAL_S = 60

NUDGE = (
    "[atlas] A large task just finished. Before you stop, post a short recap for a "
    "non-technical user, formatted exactly as:\n\n"
    "**<< Atlas Summary >>**\n\n"
    "**What changed:** 1-2 sentences on the outcome for the user -- effects and "
    "results, not files, code, or tool names.\n\n"
    "**Open questions:** anything you need the user to decide before you continue. "
    "For each, write the one-line question, then offer 2-4 lettered options "
    "(A, B, C ...) with a short label each, so the user can reply with just a "
    "letter. If you have a recommendation, mark that one option '(recommended)'. "
    "Write 'None' if there are none.\n\n"
    "Keep it brief and plain. If you already gave this recap in your reply, just stop."
)


def _state_path(repo_root: Path) -> Path:
    return repo_root / "data" / ".state" / "atlas" / "_summary.json"


def read_state(repo_root: Path) -> dict:
    p = _state_path(repo_root)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def write_state(repo_root: Path, state: dict) -> None:
    p = _state_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    atlas_checkpoint.atomic_write(p, json.dumps(state, indent=2))


def check(repo_root: Path, now: float) -> str:
    """Return the nudge text if the JUST-ENDED task was large, else "".

    "Large" is measured on the current task alone -- assistant turns since the
    last user message -- NOT accumulated since the last summary. So a small task
    never triggers a summary just because earlier small tasks piled up. Fires at
    most once per user task (guarded by that task's timestamp) and no more than
    once per cooldown window.
    """
    if not atlas_config.is_enabled(repo_root, "summary"):
        return ""
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if not agent_id:
        return ""

    paths = atlas_transcript.transcript_paths([agent_id])
    task_start = atlas_transcript.last_user_message_ts(paths)
    if task_start <= 0.0:
        return ""

    state = read_state(repo_root)
    if float(state.get("last_task_ts") or 0.0) == task_start:
        return ""  # already summarized this task (e.g. the re-engaged write turn)
    if (now - float(state.get("last_nudge_ts") or 0.0)) < SUMMARY_MIN_INTERVAL_S:
        return ""

    # The current task's size (global scope -- every large task counts, tracked or not).
    turns = atlas_transcript.activity_since(paths, task_start)["turns"]
    if turns < atlas_checkpoint.LARGE_TASK_TURNS:
        return ""

    state["last_task_ts"] = task_start
    state["last_nudge_ts"] = now
    write_state(repo_root, state)
    return NUDGE


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Atlas end-of-task chat summary.")
    parser.add_argument("--check", action="store_true", help="print the nudge if due")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()

    if args.check:
        nudge = check(repo_root, now)
        if nudge:
            print(nudge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
