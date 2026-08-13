#!/usr/bin/env python3
"""Read the agent's own responses and work -- the primary Atlas signal.

Atlas keys off what the agent actually did, not the git branch. This module
reads the common transcript(s) of the agent(s) working a topic and answers two
questions:

  - **activity-since**: how many assistant turns (and tool steps, and tokens)
    happened since a timestamp? This is the movement signal the checkpoint clock
    uses to decide the live tier is stale -- so a long, uncommitted work session
    still registers.
  - **reduce**: a compact, citation-tagged view of the conversation (the opening
    ask, decision-shaped turns, the latest turn) for generating §1/§3/§4 from
    what was said and done rather than reconstructing it from commits.

A topic's agents are resolved from its declaration, in order: explicit
`match.agent_ids` (recorded via `track-me`), then `match.agent_labels` via
`mngr list`, then -- last resort -- the current agent when the topic matches the
current branch. Transcripts are found in both the live agents dir and the
preserved (destroyed) dir, so a topic keeps its history after `mngr gc`.

Usage:
    atlas_transcript.py activity-since --slug S --since EPOCH
    atlas_transcript.py reduce --slug S [--since EPOCH] [--max-chars N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402

# Decision-shaped cues: turns containing these are worth keeping for §3/§4.
DECISION_CUES = (
    "instead",
    "decided",
    "decision",
    "won't",
    "will not",
    "because",
    "reverted",
    "turns out",
    "the problem is",
    "let's",
    "should",
    "the fix",
    "gap",
)


def host_dir() -> Path:
    return Path(os.environ.get("MNGR_HOST_DIR") or (Path.home() / ".mngr"))


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def load_declaration(repo_root: Path, slug: str) -> dict:
    return atlas_common.load_declaration(repo_root, slug)


def resolve_agent_ids(repo_root: Path, slug: str) -> list[str]:
    """Agent ids whose work counts for this topic.

    The proper association is `match.agent_labels` -- agents created with a topic
    label self-identify. Absent that, we attribute the *current* agent only when
    it is plausibly working this topic (the topic's `match.branches` includes the
    current branch), so an unrelated dormant topic never absorbs this session's
    turns. The branch is used only to route agent<->topic here -- never as the
    source of movement or content, which always come from the transcript.
    """
    decl = load_declaration(repo_root, slug)
    match = decl.get("match", {}) or {}

    # 1. Explicit association wins: agents recorded on the topic (via --track-me).
    explicit = match.get("agent_ids") or []
    if explicit:
        return [str(a) for a in explicit]

    # 2. Label-based association: agents created with a topic label.
    labels = match.get("agent_labels", {}) or {}
    if labels:
        cmd = ["mngr", "list", "--format", "json"]
        for key, value in labels.items():
            cmd += ["--label", f"{key}={value}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                ids = [a.get("id") for a in payload.get("agents", []) if a.get("id")]
                if ids:
                    return ids
            except json.JSONDecodeError:
                pass
        return []

    # 3. Last-resort fallback: the current agent, but only when the topic matches
    # the current branch (so an unrelated topic never absorbs this session).
    current = os.environ.get("MNGR_AGENT_ID")
    if not current:
        return []
    branch = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    patterns = match.get("branches", []) or []
    if branch and any(fnmatch(branch, pat) for pat in patterns):
        return [current]
    return []


def record_current_agent(repo_root: Path, slug: str) -> str | None:
    """Record the current agent's id into the topic's [match].agent_ids.

    This is the branch-free association: a topic tracks exactly the agent(s) that
    did its work. Idempotent; returns the id recorded (or None if unavailable).
    """
    aid = os.environ.get("MNGR_AGENT_ID")
    if not aid:
        return None
    path = repo_root / "atlas" / "topics" / f"{slug}.toml"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        decl = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    current = (decl.get("match", {}) or {}).get("agent_ids", []) or []
    if aid in current:
        return aid

    if re.search(r"^\s*agent_ids\s*=", text, re.MULTILINE):

        def _add(m: re.Match) -> str:
            items = [x for x in m.group(1).strip("[] ").split(",") if x.strip()]
            items.append(f'"{aid}"')
            return "agent_ids = [" + ", ".join(items) + "]"

        text = re.sub(r"agent_ids\s*=\s*(\[[^\]]*\])", _add, text, count=1)
    elif "[match]" in text:
        text = text.replace("[match]", f'[match]\nagent_ids = ["{aid}"]', 1)
    else:
        text = text.rstrip() + f'\n\n[match]\nagent_ids = ["{aid}"]\n'
    path.write_text(text, encoding="utf-8")
    return aid


def transcript_paths(agent_ids: list[str]) -> list[Path]:
    """The claude common-transcript for each agent, live or preserved."""
    root = host_dir()
    paths: list[Path] = []
    for agent_id in agent_ids:
        live = (
            root
            / "agents"
            / agent_id
            / "events"
            / "claude"
            / "common_transcript"
            / "events.jsonl"
        )
        if live.is_file():
            paths.append(live)
        # Preserved dirs are named <agent_name>--<agent_id>; match by id suffix.
        preserved = root / "preserved"
        if preserved.is_dir():
            for d in preserved.glob(f"*{agent_id}"):
                cand = d / "events" / "claude" / "common_transcript" / "events.jsonl"
                if cand.is_file():
                    paths.append(cand)
    return paths


_STOPWORDS = {
    "the",
    "and",
    "app",
    "for",
    "with",
    "into",
    "from",
    "this",
    "that",
    "atlas",
    "system",
    "apps",
    "src",
    "test",
    "tests",
    "page",
    "topic",
    "work",
}


def topic_keywords(repo_root: Path, slug: str) -> set[str]:
    """Lowercased keywords identifying a topic, for scoping turns to it.

    Drawn from the slug, the title, any explicit `match.keywords`, and the
    basenames of matched paths -- so a turn "belongs" to the topic when it
    mentions what the topic is about. Empty set -> no scoping (all turns count).
    """
    decl = load_declaration(repo_root, slug)
    match = decl.get("match", {}) or {}
    words: set[str] = set()
    for token in re.split(r"[^a-z0-9]+", slug.lower()):
        words.add(token)
    for token in re.split(r"[^a-z0-9]+", str(decl.get("title", "")).lower()):
        words.add(token)
    for kw in match.get("keywords", []) or []:
        words.add(str(kw).lower())
    for path in match.get("paths", []) or []:
        base = re.split(r"[/*]+", str(path).rstrip("/*"))[-1]
        for token in re.split(r"[^a-z0-9]+", base.lower()):
            words.add(token)
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _event_text(event: dict) -> str:
    parts = [str(event.get("content") or ""), str(event.get("text") or "")]
    if event.get("type") == "tool_result":
        parts.append(str(event.get("tool_name") or ""))
        parts.append(str(event.get("output") or "")[:2000])
    return " ".join(parts).lower()


def _matches(event: dict, keywords: set[str]) -> bool:
    """True if the event is on-topic (or no keywords given -> everything is).

    Word-boundary match, so 'notes' does not fire inside 'footnotes'. Keyword
    scoping is a heuristic: generic single-word slugs (e.g. 'notes') still cause
    false positives -- sharpen a topic with a distinctive `match.keywords`.
    """
    if not keywords:
        return True
    text = _event_text(event)
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in keywords)


def iter_events(paths: list[Path]):
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def activity_since(
    paths: list[Path], since_epoch: float, keywords: set[str] | None = None
) -> dict:
    """Count assistant turns, tool steps, and tokens after `since_epoch`.

    When `keywords` is given, only on-topic turns count -- so a topic sees its
    own work, not every turn of an agent that also worked other topics.
    """
    turns = tools = tokens = 0
    last_ts = 0.0
    for event in iter_events(paths):
        ts = _iso_to_epoch(event.get("timestamp"))
        if ts <= since_epoch:
            continue
        if not _matches(event, keywords or set()):
            continue
        etype = event.get("type")
        if etype == "assistant_message":
            turns += 1
            usage = event.get("usage") or {}
            tokens += int(usage.get("output_tokens", 0) or 0)
        elif etype == "tool_result":
            tools += 1
        last_ts = max(last_ts, ts)
    return {"turns": turns, "tools": tools, "tokens": tokens, "last_ts": last_ts}


def last_user_message_ts(paths: list[Path]) -> float:
    """Epoch of the most recent user message across the transcript(s), or 0.0.

    Marks the start of the current task: assistant turns after it are the work
    done for the latest request.
    """
    last = 0.0
    for event in iter_events(paths):
        if event.get("type") == "user_message":
            ts = _iso_to_epoch(event.get("timestamp"))
            if ts > last:
                last = ts
    return last


def status_activity(
    paths: list[Path], checkpoint: float | None, keywords: set[str] | None = None
) -> dict:
    """One transcript pass for the §0 line: last on-topic event and turns-since.

    Returns {last_ts, turns_since_checkpoint}. `turns_since_checkpoint` counts
    assistant turns after `checkpoint` (or all turns when checkpoint is None).
    Replaces two full passes (all-time + since-checkpoint) with one.
    """
    since = checkpoint if checkpoint is not None else 0.0
    last_ts = 0.0
    turns = 0
    for event in iter_events(paths):
        if not _matches(event, keywords or set()):
            continue
        ts = _iso_to_epoch(event.get("timestamp"))
        if ts > last_ts:
            last_ts = ts
        if event.get("type") == "assistant_message" and ts > since:
            turns += 1
    return {"last_ts": last_ts, "turns_since_checkpoint": turns}


def _text_of(event: dict) -> str:
    if event.get("type") == "user_message":
        return str(event.get("content") or "")
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "")
    return ""


def reduce(
    paths: list[Path],
    since_epoch: float,
    max_chars: int,
    keywords: set[str] | None = None,
) -> str:
    """A compact, citation-tagged view for generating §1/§3/§4.

    Keeps the first user ask, decision-shaped assistant turns, and the latest
    turn. Each kept turn is tagged with its event_id + timestamp so a generated
    claim can cite `transcript:<event_id>` and store a verbatim quote. When
    `keywords` is given, only on-topic turns are considered.
    """
    events = [
        e
        for e in iter_events(paths)
        if _iso_to_epoch(e.get("timestamp")) > since_epoch
        and _matches(e, keywords or set())
    ]
    events.sort(key=lambda e: _iso_to_epoch(e.get("timestamp")))
    if not events:
        return "(no transcript activity in range)"

    kept: list[dict] = []
    for event in events:
        etype = event.get("type")
        if etype == "user_message":
            # Every user ask is kept -- the asks are half the story of the work.
            kept.append(event)
        elif etype == "assistant_message":
            text = _text_of(event).lower()
            if any(cue in text for cue in DECISION_CUES):
                kept.append(event)
    last_asst = next(
        (e for e in reversed(events) if e.get("type") == "assistant_message"), None
    )
    if last_asst and last_asst not in kept:
        kept.append(last_asst)

    # De-dupe, preserve order.
    seen = set()
    ordered = []
    for event in kept:
        uid = event.get("event_id")
        if uid in seen:
            continue
        seen.add(uid)
        ordered.append(event)

    out: list[str] = []
    budget = max_chars
    for event in ordered:
        role = "USER" if event.get("type") == "user_message" else "ASSISTANT"
        when = str(event.get("timestamp", ""))[:19]
        eid = event.get("event_id", "?")
        text = _text_of(event).strip()
        if len(text) > 600:
            text = text[:600] + " …"
        block = f"[{role} {when} transcript:{eid}]\n{text}\n"
        if budget - len(block) < 0:
            out.append(
                f"… (reduction truncated at {max_chars} chars; more turns omitted)"
            )
            break
        out.append(block)
        budget -= len(block)
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Read the agent's transcript for Atlas."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("activity-since", help="count turns/tools/tokens since an epoch")
    a.add_argument("--slug", required=True)
    a.add_argument("--since", type=float, required=True)
    a.add_argument("--repo-root", default=None)
    a.add_argument(
        "--no-scope", action="store_true", help="count all turns, not just on-topic"
    )

    r = sub.add_parser("reduce", help="print a citation-tagged reduced transcript")
    r.add_argument("--slug", required=True)
    r.add_argument("--since", type=float, default=0.0)
    r.add_argument("--max-chars", type=int, default=12000)
    r.add_argument("--repo-root", default=None)
    r.add_argument(
        "--no-scope", action="store_true", help="include all turns, not just on-topic"
    )

    t = sub.add_parser("track-me", help="record the current agent id on the topic")
    t.add_argument("--slug", required=True)
    t.add_argument("--repo-root", default=None)

    w = sub.add_parser("which-agents", help="print the agent ids resolved for a topic")
    w.add_argument("--slug", required=True)
    w.add_argument("--repo-root", default=None)

    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)

    if args.cmd == "track-me":
        recorded = record_current_agent(repo_root, args.slug)
        print(
            f"atlas: recorded agent {recorded} on topic {args.slug}"
            if recorded
            else "atlas: no current agent to record"
        )
        return 0

    ids = resolve_agent_ids(repo_root, args.slug)
    if args.cmd == "which-agents":
        print(json.dumps(ids))
        return 0
    paths = transcript_paths(ids)
    kw = (
        set()
        if getattr(args, "no_scope", False)
        else topic_keywords(repo_root, args.slug)
    )

    if args.cmd == "activity-since":
        print(json.dumps(activity_since(paths, args.since, kw)))
    elif args.cmd == "reduce":
        print(reduce(paths, args.since, args.max_chars, kw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
