#!/usr/bin/env python3
"""On-the-clock live-tier refresh -- the out-of-band worker (decision 1).

Regenerates §1 (current state) and §7 (next steps) from the agent's recent
transcript, with a cheap model, without spending the working agent's context.
The checkpoint clock spawns this detached when a topic has moved; it can also be
run by hand. Guardrails (decision 6):

  - **Opt-in per topic**: only runs if the declaration sets `live_model = true`
    (override with --force).
  - **Per-topic hourly token ceiling** (default 50k): if the last hour's
    live-refresh output tokens exceed it, it logs a downgrade and leaves §1/§7
    to the next full `/atlas` generation (§0 still refreshes for free).
  - **Cheap model** (Haiku).

The auto-refreshed §1/§7 carry an inline "live refresh" provenance note rather
than per-sentence citations; rigorous citations come from a full `/atlas <slug>`
generation. §2 and pinned blocks are never touched.

Usage:
    atlas_live_refresh.py --slug S [--repo-root R] [--force] [--now EPOCH]
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_ai  # noqa: E402
import atlas_checkpoint  # noqa: E402
import atlas_common  # noqa: E402
import atlas_transcript  # noqa: E402

# Per-topic hourly ceiling on live-refresh output tokens (decision 6). Cost is
# also logged per fire for reference.
HOURLY_TOKEN_CEILING = 50_000

SYSTEM = (
    "You keep a one-page project summary current. Given the agent's recent work, "
    "you write two short sections a returning teammate reads to catch up: the "
    "CURRENT STATE (present tense, no dates, <=120 words) and NEXT STEPS (concrete, "
    "checkable, <=80 words). Be factual and specific; do not invent."
)

PROMPT = """\
Topic: {title}

Why this topic exists (do not contradict):
{why}

The current page's sections (may be stale):
CURRENT STATE:
{cur_state}
NEXT STEPS:
{cur_next}

The agent's recent work since the last refresh:
{recent}

Rewrite the two sections from the recent work. Respond with STRICT JSON, no prose:
  {{"current_state": "<=120 words, present tense, no dates",
    "next_steps": "- bullet\\n- bullet  (<=80 words total)"}}
"""


def replace_section(text: str, header: str, new_body: str) -> str:
    """Replace the body between a `## header` line and the next section boundary."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return text
    end = atlas_common.section_end(lines, start)
    rebuilt = lines[: start + 1] + ["", new_body.rstrip(), ""] + lines[end:]
    return "\n".join(rebuilt)


def _section_body(text: str, header: str) -> str:
    """The body between `## header` and the next section boundary (shared impl)."""
    return atlas_common.section_body(text, header)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh a topic's live tier with a model."
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument(
        "--force", action="store_true", help="ignore opt-in and ceiling"
    )
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()

    try:
        decl = atlas_common.load_declaration(repo_root, args.slug, missing_ok=False)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"atlas_live_refresh: {exc}", file=sys.stderr)
        return 0

    if not args.force and not decl.get("live_model"):
        print(f"atlas_live_refresh: {args.slug} not opted in (live_model); skip")
        return 0

    if not args.force:
        spent = atlas_common.tokens_last_hour(repo_root, args.slug, now, "live_refresh")
        if spent >= HOURLY_TOKEN_CEILING:
            atlas_checkpoint.log_event(
                repo_root,
                args.slug,
                {
                    "ts": round(now, 3),
                    "reason": "live_downgrade",
                    "tokens_last_hour": spent,
                },
            )
            print(
                f"atlas_live_refresh: {args.slug} over {HOURLY_TOKEN_CEILING} tok/hr "
                f"ceiling ({spent}); downgraded to §0-only"
            )
            return 0

    state = atlas_checkpoint.read_state(repo_root, args.slug)
    since = float((state.get("live") or {}).get("last_refresh") or 0.0)
    paths = atlas_transcript.transcript_paths(
        atlas_transcript.resolve_agent_ids(repo_root, args.slug)
    )
    kw = atlas_transcript.topic_keywords(
        repo_root, args.slug
    )  # scope §1/§7 to the topic
    recent = atlas_transcript.reduce(paths, since, max_chars=8000, keywords=kw)
    if recent.startswith("(no transcript"):
        print(f"atlas_live_refresh: {args.slug} no new work; skip")
        return 0

    # No page yet (e.g. an auto-proposed topic before its first generation):
    # skip BEFORE spending tokens, and leave the pending flag set so a later
    # full `/atlas` generation is still prompted.
    page_path = atlas_common.page_path(repo_root, args.slug)
    if not page_path.is_file():
        print(f"atlas_live_refresh: {args.slug} has no page yet; skip")
        return 0
    page = page_path.read_text(encoding="utf-8")
    if "## Current state" not in page and "## Next steps" not in page:
        print(f"atlas_live_refresh: {args.slug} page has no live sections; skip")
        return 0
    prompt = PROMPT.format(
        title=decl.get("title", args.slug),
        why=_section_body(page, "## Why this exists") or "(unspecified)",
        cur_state=_section_body(page, "## Current state"),
        cur_next=_section_body(page, "## Next steps"),
        recent=recent,
    )

    try:
        result = atlas_ai.complete(prompt, system=SYSTEM)
    except atlas_ai.AIUnavailable as exc:
        print(f"atlas_live_refresh: model unavailable ({exc}); skip", file=sys.stderr)
        return 0

    # Route through the shared strong parser: it strips ``` fences AND recovers a
    # `{...}` span from a chattier reply the old inline json.loads gave up on, and
    # its dict guard keeps a non-object reply from crashing this detached worker.
    verdict = atlas_common.parse_model_json(result["text"])
    if not verdict:
        print("atlas_live_refresh: could not parse model JSON; skip", file=sys.stderr)
        return 0

    # No date in the note: §1 must stay dateless, and the note lands in §1.
    note = "\n*(auto-refreshed from recent work; full citations added on the next full write-up)*"
    new_state = str(verdict.get("current_state", "")).strip()
    new_next = str(verdict.get("next_steps", "")).strip()
    if not new_state and not new_next:
        # Empty model output: nothing to splice. Do NOT clear_live -- leave the
        # pending flag set so this movement is retried, rather than silently
        # swallowing it and leaving §1/§7 stale.
        print(
            f"atlas_live_refresh: {args.slug} model returned no sections; left pending"
        )
        return 0

    # Splice under the per-slug lock, re-reading the page inside it so a
    # concurrent §0 checkpoint splice (or a human edit) is not clobbered by a
    # stale copy read before the (seconds-long) model call.
    lock_dir = atlas_checkpoint.state_dir(repo_root, args.slug)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        page = page_path.read_text(encoding="utf-8")
        if new_state:
            page = replace_section(page, "## Current state", new_state + note)
        if new_next:
            page = replace_section(page, "## Next steps", new_next + note)
        atlas_checkpoint.atomic_write(page_path, page)

    atlas_checkpoint.log_event(
        repo_root,
        args.slug,
        {
            "ts": round(now, 3),
            "reason": "live_refresh",
            "tier": "live",
            "tokens": result.get("output_tokens", 0),
            "cost_usd": result.get("cost_usd"),
        },
    )
    # Reset the movement baseline: this refresh consumed the pending work.
    atlas_checkpoint.clear_live(repo_root, args.slug, now)
    print(
        f"atlas_live_refresh: {args.slug} refreshed §1/§7 "
        f"({result.get('output_tokens')} tokens, ${result.get('cost_usd')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
