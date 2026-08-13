#!/usr/bin/env python3
"""Generate the Atlas §0 status line for a topic -- deterministically, no model.

The status line is the cheapest tier of an Atlas page: it is meant to be
recomputed on every read, so it must stay fast and never call an LLM. Its primary
signal is the agent's own work (its transcript), not the git branch. It reads:

  - the topic declaration  atlas/topics/<slug>.toml   (status + match rules)
  - the agent transcript   turns + last-active for the topic's agent(s)
  - the live agent state    `mngr list`                (only if the topic
                            declares agent_labels)
  - the checkpoint marker   data/.state/atlas/<slug>/last_checkpoint
  - git                     a `last commit` fact, only when a topic is dormant
                            (no agent, no transcript activity)

It prints a single line to stdout. Shapes:

  RUNNING · last active 40s ago · checkpointed 2m ago · 3 turns since
  PROPOSED · last active 8s ago · checkpointed never · 447 turns since
  SHIPPED · dormant · last commit 9474a4fb (2026-08-11) · checkpointed never

Usage:
    atlas_status.py <slug> [--repo-root DIR]

Exit codes: 0 on success; 2 if the declaration is missing or malformed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402

# Where mngr keeps per-agent state on this host. find-transcripts documents the
# same root; honor an override the way the rest of the template does.
MNGR_HOST_DIR = Path(
    subprocess.run(
        ["bash", "-lc", 'echo "${MNGR_HOST_DIR:-$HOME/.mngr}"'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    or str(Path.home() / ".mngr")
)


class DeclarationError(Exception):
    """The topic declaration is missing or does not parse."""


def _run(args: list[str], cwd: Path) -> str:
    """Run a command, returning stripped stdout ('' on any nonzero exit)."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _rel_time(then_epoch: float, now_epoch: float) -> str:
    """Render an elapsed time as a compact 'Xs/Xm/Xh/Xd ago'."""
    delta = max(0, int(now_epoch - then_epoch))
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def load_declaration(repo_root: Path, slug: str) -> dict:
    decl_path = repo_root / "atlas" / "topics" / f"{slug}.toml"
    if not decl_path.is_file():
        raise DeclarationError(f"no declaration at {decl_path}")
    try:
        return atlas_common.load_declaration(repo_root, slug, missing_ok=False)
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"malformed declaration {decl_path}: {exc}") from exc


def read_checkpoint(repo_root: Path, slug: str) -> float | None:
    """Return the last-checkpoint epoch, or None if never checkpointed."""
    marker = atlas_common.state_dir(repo_root, slug) / "last_checkpoint"
    if not marker.is_file():
        return None
    raw = marker.read_text(encoding="utf-8").strip()
    if not raw:
        # Present but empty: fall back to the file's own mtime.
        return marker.stat().st_mtime
    try:
        return float(raw)
    except ValueError:
        return marker.stat().st_mtime


def _match_branches(repo_root: Path, patterns: list[str]) -> list[str]:
    """Expand branch glob patterns to concrete local branch names."""
    branches: list[str] = []
    for pattern in patterns:
        out = _run(
            ["git", "branch", "--list", "--format=%(refname:short)", pattern], repo_root
        )
        branches.extend(line for line in out.splitlines() if line)
    # Dedupe, order-stable.
    return list(dict.fromkeys(branches))


def _iso_to_epoch(value: str | None) -> float:
    """Parse an ISO-8601 timestamp (mngr uses trailing 'Z') to epoch, 0 on failure."""
    if not value:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _agent_activity_epoch(agent: dict) -> float:
    """Best available 'last active' epoch for an agent.

    The per-agent `activity` file mtime is the most reliable signal; fall back to
    the JSON activity/start timestamps so the line degrades to a real time rather
    than 'unknown' when the file is absent.
    """
    act = MNGR_HOST_DIR / "agents" / str(agent.get("id", "")) / "activity"
    if act.is_file():
        return act.stat().st_mtime
    for field in (
        "agent_activity_time",
        "user_activity_time",
        "start_time",
        "create_time",
    ):
        epoch = _iso_to_epoch(agent.get(field))
        if epoch:
            return epoch
    return 0.0


def _find_live_agent(agent_ids: list[str]) -> dict | None:
    """Most-recently-active agent among `agent_ids` (via mngr), or None.

    Resolves live state for whichever agents actually work the topic -- however
    they were associated (agent_ids, agent_labels, or the branch fallback), all
    of which resolve_agent_ids has already reduced to ids -- so §0 shows
    RUNNING/WAITING even for topics tied by agent_ids rather than labels.
    """
    if not agent_ids:
        return None
    raw = _run(["mngr", "list", "--format", "json"], Path.cwd())
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    agents = payload.get("agents", []) if isinstance(payload, dict) else []
    wanted = set(agent_ids)
    matching = [a for a in agents if a.get("id") in wanted]
    present = [a for a in matching if a.get("state") in {"RUNNING", "WAITING"}]
    candidates = present or matching
    if not candidates:
        return None
    return max(candidates, key=_agent_activity_epoch)


def build_status_line(repo_root: Path, slug: str, now_epoch: float) -> str:
    decl = load_declaration(repo_root, slug)
    status = str(decl.get("status", "unknown")).upper()
    match = decl.get("match", {}) or {}
    branches = _match_branches(repo_root, list(match.get("branches", []))) or ["HEAD"]
    paths = list(match.get("paths", []))

    checkpoint = read_checkpoint(repo_root, slug)
    checkpoint_str = "never" if checkpoint is None else _rel_time(checkpoint, now_epoch)

    # The primary signal is the agent's own work, read from its transcript --
    # not the git branch. `turns since` counts assistant turns since the last
    # checkpoint; `last active` is the most recent transcript event.
    import atlas_transcript  # local import avoids a module-load cycle

    tx_ids = atlas_transcript.resolve_agent_ids(repo_root, slug)
    tx_paths = atlas_transcript.transcript_paths(tx_ids)
    kw = atlas_transcript.topic_keywords(repo_root, slug)  # scope to this topic
    # Single transcript pass for both the last-active time and turns-since.
    summary = atlas_transcript.status_activity(tx_paths, checkpoint, kw)
    turns_since = summary["turns_since_checkpoint"]

    agent = _find_live_agent(tx_ids)
    topic_last = summary["last_ts"]  # last ON-TOPIC event (keyword-scoped)
    if agent is not None:
        # The agent may be live (RUNNING/WAITING) while this particular topic has
        # not been touched recently -- so "last active" is the topic's last
        # on-topic event, NOT the agent's overall activity. Otherwise every topic
        # tied to a busy agent would show the same fresh time.
        state = str(agent.get("state", "UNKNOWN"))
        active_epoch = topic_last or _agent_activity_epoch(agent)
    elif topic_last:
        # No live agent record, but the topic has transcript activity: report
        # from that work (declared status + last on-topic event).
        state = status
        active_epoch = topic_last
    else:
        # Truly dormant: no agent, no transcript. Fall back to a git fact.
        log_args = ["git", "log", "-1", "--format=%h (%cs)", *branches]
        if paths:
            log_args += ["--", *paths]
        last = _run(log_args, repo_root) or "none"
        return (
            f"{status} · dormant · last commit {last} · checkpointed {checkpoint_str}"
        )

    active_str = _rel_time(active_epoch, now_epoch) if active_epoch else "unknown"
    # Computed staleness (turns since last full generation) -- shown in §0 so it
    # is unmissable. No model; degrades to "" if evidence tooling is unavailable.
    stale_suffix = ""
    try:
        import atlas_evidence

        stale_suffix = atlas_evidence.staleness_suffix(repo_root, slug)
    except Exception:
        stale_suffix = ""
    return (
        f"{state} · last active {active_str} · "
        f"checkpointed {checkpoint_str} · {turns_since} turns since{stale_suffix}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate an Atlas §0 status line.")
    parser.add_argument("slug", help="topic slug (matches atlas/topics/<slug>.toml)")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root (default: git toplevel of the cwd)",
    )
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)

    try:
        line = build_status_line(repo_root, args.slug, time.time())
    except DeclarationError as exc:
        print(f"atlas_status: {exc}", file=sys.stderr)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
