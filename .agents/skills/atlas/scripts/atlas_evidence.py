#!/usr/bin/env python3
"""Provenance + computed staleness for an Atlas page.

Since content is transcript-driven, staleness is simply how much *work* has
happened since the page was last fully generated: assistant turns in the topic's
agent transcript after the recorded generation time. Computed, never asserted,
and cheap enough to run on every read -- no model.

`record` (run by the skill after a full `/atlas <slug>` generation) writes
`atlas/<slug>.evidence.json`: when it was generated, which agents, the set of
cited sources, and a digest. `check` reports whether the page is stale and by how
much. The §0 status line surfaces staleness so it is unmissable.

Usage:
    atlas_evidence.py record --slug S [--repo-root R]
    atlas_evidence.py check  --slug S [--repo-root R] [--stale-turns N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402
import atlas_transcript  # noqa: E402

DEFAULT_STALE_TURNS = 8


def evidence_path(repo_root: Path, slug: str) -> Path:
    return repo_root / "atlas" / f"{slug}.evidence.json"


def _cited_ids(page_text: str) -> list[str]:
    return sorted(set(re.findall(r"^\[\^([A-Za-z0-9_-]+)\]:", page_text, re.MULTILINE)))


def record(repo_root: Path, slug: str, now: float) -> dict:
    page = atlas_common.page_path(repo_root, slug)
    text = page.read_text(encoding="utf-8") if page.is_file() else ""
    cited = _cited_ids(text)
    words = len(text.split())
    digest = hashlib.sha1(("|".join(cited) + f"|{words}").encode()).hexdigest()[:12]
    data = {
        "slug": slug,
        "generated_ts": round(now, 3),
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        "agent_ids": atlas_transcript.resolve_agent_ids(repo_root, slug),
        "cited": cited,
        "words": words,
        "evidence_digest": digest,
    }
    atlas_common.atomic_write(
        evidence_path(repo_root, slug), json.dumps(data, indent=2)
    )
    return data


def check(repo_root: Path, slug: str, stale_turns: int) -> dict:
    p = evidence_path(repo_root, slug)
    if not p.is_file():
        return {"stale": True, "reason": "never generated", "new_turns": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"stale": True, "reason": "unreadable evidence", "new_turns": None}
    gen_ts = float(data.get("generated_ts") or 0.0)
    paths = atlas_transcript.transcript_paths(
        atlas_transcript.resolve_agent_ids(repo_root, slug)
    )
    kw = atlas_transcript.topic_keywords(repo_root, slug)  # scope to this topic
    new_turns = atlas_transcript.activity_since(paths, gen_ts, kw)["turns"]
    return {
        "stale": new_turns >= stale_turns,
        "new_turns": new_turns,
        "generated_at": data.get("generated_at"),
        "stale_turns_threshold": stale_turns,
    }


def staleness_suffix(repo_root: Path, slug: str) -> str:
    """A short §0 suffix when the page is stale, else ''. Used by atlas_status."""
    result = check(repo_root, slug, DEFAULT_STALE_TURNS)
    if not result.get("stale"):
        return ""
    if result.get("new_turns") is None:
        return " · ⚠ never generated"
    return f" · ⚠ stale: {result['new_turns']} turns since generation"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Atlas provenance and staleness.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("record", "check"):
        s = sub.add_parser(name)
        s.add_argument("--slug", required=True)
        s.add_argument("--repo-root", default=None)
        s.add_argument("--now", type=float, default=None)
        if name == "check":
            s.add_argument("--stale-turns", type=int, default=DEFAULT_STALE_TURNS)
    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()

    if args.cmd == "record":
        data = record(repo_root, args.slug, now)
        print(
            f"atlas_evidence: recorded {args.slug} generation ({data['words']} words, {len(data['cited'])} sources)"
        )
    elif args.cmd == "check":
        print(json.dumps(check(repo_root, args.slug, args.stale_turns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
