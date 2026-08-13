#!/usr/bin/env python3
"""Background full-page generation -- the end-of-task rich tier (A1).

Live mode keeps §1/§7 current cheaply *during* a task. When a large task ends,
the checkpoint clock spawns this worker to regenerate the WHOLE page -- §1, §3,
§4, §5, §6, §7 plus an Evidence table -- from the topic's transcript, with a
cheap model, detached so the agent is never blocked. It is the automatic backstop
so a page is never left a skeleton after a big task; a human-run `/atlas <slug>`
still produces the gold-standard, agent-written cited page.

What it guarantees:
  - **Never overwrites human edits.** §2 (Why this exists) is copied through, and
    if the page carries any `<!-- atlas:pinned -->` block the worker SKIPS and
    leaves the page to a human `/atlas` run. The page is re-read under the lock
    after the model call, so a pin or §2 edit made *during* the call still wins.
  - **Citations resolve.** Every source the model may cite comes from a numbered
    menu built from the reduced transcript; markers outside the menu -- whether
    the model invented them or they dangle in the copied §2 -- are stripped, so
    validation always passes on citation resolution.
  - **Bounded spend.** A per-topic hourly token ceiling stops a topic whose
    output never parses from respawning a paid call every interval.
  - **Opt-in.** Only runs for topics with `live_model = true` (override --force).

Usage:
    atlas_generate.py --slug S [--repo-root R] [--force] [--now EPOCH]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_ai  # noqa: E402
import atlas_checkpoint  # noqa: E402
import atlas_common  # noqa: E402
import atlas_evidence  # noqa: E402
import atlas_index  # noqa: E402
import atlas_status  # noqa: E402
import atlas_transcript  # noqa: E402
import atlas_validate  # noqa: E402

# How much reduced transcript to feed the model (whole-topic history for §3).
REDUCE_CHARS = 12000

# Per-topic hourly ceiling on full-generation output tokens. Bounds a runaway
# retry loop (a topic whose model output never parses would otherwise respawn a
# paid call every interval); mirrors the live worker's ceiling.
HOURLY_TOKEN_CEILING = 50_000

SYSTEM = (
    "You write a single-page catch-up summary of a software feature from an AI "
    "agent's own worklog. A returning developer reads it instead of scrolling the "
    "transcript. Be factual and specific; invent nothing. Cite claims with the "
    "footnote ids you are given (e.g. [^t3]); never invent a footnote id. §1 is "
    "present tense with NO dates; §3 is past tense with every entry dated."
)

PROMPT = """\
Feature: {title}

Why this feature exists (do not contradict; this is fixed):
{why}

Sources you may cite -- use these footnote ids only, e.g. [^t2]:
{menu}

The agent's work on this feature (reduced transcript):
{reduced}

Write the page as STRICT JSON, no prose outside it. Cite claims in §1/§3/§4/§5
with the footnote ids above. Respect the word budgets:
  {{"current_state": "<=120 words, present tense, NO dates, with [^id] cites",
    "how_it_got_here": "<=200 words, dated bullets newest last, with [^id] cites",
    "decisions": "<=200 words, one bullet each: decision - rationale [^id]",
    "implementation_shape": "<=150 words, the files/pieces that matter [^id]",
    "open_questions": "<=100 words, each with what would resolve it",
    "next_steps": "- concrete, checkable bullets (<=80 words total)"}}
"""

MENU_HEADER = re.compile(r"\[(USER|ASSISTANT) ([^\]]+?) transcript:([^\]]+)\]\n")


def _section_body(text: str, header: str) -> str:
    """The body between `## header` and the next section boundary (shared impl)."""
    return atlas_common.section_body(text, header)


def build_menu(reduced: str) -> list[dict]:
    """Parse the reduced transcript into citable sources: id, event_id, quote."""
    matches = list(MENU_HEADER.finditer(reduced))
    menu: list[dict] = []
    for i, m in enumerate(matches, start=1):
        body_start = m.end()
        body_end = matches[i].start() if i < len(matches) else len(reduced)
        raw = reduced[body_start:body_end].strip()
        quote = re.sub(r"\s+", " ", raw)[:180].strip()
        menu.append(
            {
                "id": f"t{i}",
                "event_id": m.group(3),
                "when": m.group(2)[:10],
                "quote": quote,
            }
        )
    return menu


def assemble_page(
    title: str,
    status_line: str,
    banner: str,
    why: str,
    sections: dict,
    used_menu: list[dict],
) -> str:
    """Build the full page markdown from generated sections + footnote sources.

    Citations are emitted as footnote *definitions* (`[^id]: source — "quote"`),
    which the viewer renders as a linked Sources list at the bottom -- no separate
    Evidence table.
    """
    parts = [f"# {title}\n"]
    if banner:
        parts.append(banner + "\n")
    parts.append("<!-- atlas:status -->\n" + status_line + "\n<!-- /atlas:status -->\n")
    ordered = [
        ("## Current state", sections.get("current_state", "")),
        ("## Why this exists", why),
        ("## How it got here", sections.get("how_it_got_here", "")),
        ("## Decisions", sections.get("decisions", "")),
        ("## Implementation shape", sections.get("implementation_shape", "")),
        ("## Open questions", sections.get("open_questions", "")),
        ("## Next steps", sections.get("next_steps", "")),
    ]
    for header, body in ordered:
        parts.append(f"{header}\n\n{(body or '(none)').strip()}\n")

    if used_menu:
        # build_menu already collapsed each quote's whitespace, so it is a single
        # clean line safe for a footnote definition.
        defs = "\n".join(
            f"[^{m['id']}]: transcript:`{m['event_id']}` ({m['when']}) — "
            f'"{m["quote"]}".'
            for m in used_menu
        )
        parts.append(defs)
    return "\n".join(parts) + "\n"


def resolve_citations(sections: dict, menu: list[dict]) -> tuple[dict, list[dict]]:
    """Keep only citations that map to a menu id; strip invented ones.

    Guarantees every remaining `[^id]` resolves to an Evidence footnote, so the
    validator's citation check always passes. Returns (clean_sections, used_menu).
    """
    valid_ids = {m["id"] for m in menu}
    used: set[str] = set()
    clean: dict[str, str] = {}
    for key, body in sections.items():
        body = str(body or "")

        def _keep(match: re.Match) -> str:
            cid = match.group(1)
            if cid in valid_ids:
                used.add(cid)
                return match.group(0)
            return ""  # strip a marker the model invented

        clean[key] = re.sub(r"\[\^([A-Za-z0-9_-]+)\](?!:)", _keep, body)
    used_menu = [m for m in menu if m["id"] in used]
    return clean, used_menu


def _parse_sections(text: str) -> dict:
    """Parse a JSON object out of the model reply; {} unless it is an object.

    Thin wrapper over the shared parser (kept for the tests that call it).
    """
    return atlas_common.parse_model_json(text)


def generate(repo_root: Path, slug: str, now: float, force: bool) -> dict:
    try:
        decl = atlas_common.load_declaration(repo_root, slug, missing_ok=False)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"slug": slug, "generated": False, "reason": f"no declaration ({exc})"}

    if not force and not decl.get("live_model"):
        return {"slug": slug, "generated": False, "reason": "not opted in (live_model)"}

    page_path = atlas_common.page_path(repo_root, slug)
    existing = page_path.read_text(encoding="utf-8") if page_path.is_file() else ""

    # Fast-path skip before spending a model call: never clobber a human edit, so
    # if the page already has pinned blocks leave it to a human-run `/atlas`. The
    # page is re-read and re-checked under the lock after the (slow) model call,
    # in case a pin or §2 edit lands while the call is in flight.
    if "<!-- atlas:pinned" in existing:
        return {"slug": slug, "generated": False, "reason": "page has pinned blocks"}

    # Per-topic hourly spend ceiling: stop a topic whose output never parses from
    # respawning a paid call every interval.
    if not force:
        spent = atlas_common.tokens_last_hour(repo_root, slug, now, "full_generate")
        if spent >= HOURLY_TOKEN_CEILING:
            return {
                "slug": slug,
                "generated": False,
                "reason": f"over {HOURLY_TOKEN_CEILING} tok/hr ceiling ({spent})",
            }

    paths = atlas_transcript.transcript_paths(
        atlas_transcript.resolve_agent_ids(repo_root, slug)
    )
    kw = atlas_transcript.topic_keywords(repo_root, slug)
    reduced = atlas_transcript.reduce(paths, 0.0, max_chars=REDUCE_CHARS, keywords=kw)
    if reduced.startswith("(no transcript"):
        return {"slug": slug, "generated": False, "reason": "no transcript work"}

    menu = build_menu(reduced)
    why_prompt = _section_body(existing, "## Why this exists") or str(
        decl.get("why") or ""
    )
    menu_str = (
        "\n".join(f'[^{m["id"]}] ({m["when"]}) "{m["quote"]}"' for m in menu)
        or "(no citable turns)"
    )
    prompt = PROMPT.format(
        title=decl.get("title", slug),
        why=why_prompt or "(unspecified)",
        menu=menu_str,
        reduced=reduced,
    )

    try:
        result = atlas_ai.complete(prompt, system=SYSTEM)
    except atlas_ai.AIUnavailable as exc:
        return {
            "slug": slug,
            "generated": False,
            "reason": f"model unavailable ({exc})",
        }

    tokens = result.get("output_tokens", 0)
    sections = _parse_sections(result["text"])
    if not sections:
        # Record the spend so repeated parse failures accrue toward the ceiling
        # above rather than looping forever on a paid call.
        atlas_checkpoint.log_event(
            repo_root,
            slug,
            {
                "ts": round(now, 3),
                "reason": "full_generate",
                "tier": "full",
                "tokens": tokens,
                "cost_usd": result.get("cost_usd"),
                "error": "unparseable",
            },
        )
        return {"slug": slug, "generated": False, "reason": "unparseable model output"}

    clean_sections, used_a = resolve_citations(sections, menu)

    banner = ""
    if decl.get("status") == "proposed":
        banner = (
            "> **Unconfirmed** — auto-created from recent work; not yet confirmed "
            "as a tracked topic."
        )

    lock_dir = atlas_checkpoint.state_dir(repo_root, slug)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Re-read inside the lock: a human may have pinned the page or edited §2
        # during the seconds-long model call. Honour that edit rather than the
        # pre-call snapshot.
        fresh = page_path.read_text(encoding="utf-8") if page_path.is_file() else ""
        if "<!-- atlas:pinned" in fresh:
            return {
                "slug": slug,
                "generated": False,
                "reason": "page gained pinned blocks during generation",
            }
        why_raw = (
            _section_body(fresh, "## Why this exists")
            or str(decl.get("why") or "")
            or "(unspecified)"
        )
        # §2 is copied through, but any citation markers in it are resolved
        # against the NEW menu (dangling ones stripped) so validation passes.
        clean_why, used_b = resolve_citations({"why": why_raw}, menu)
        used_ids = {m["id"] for m in used_a} | {m["id"] for m in used_b}
        used_menu = [m for m in menu if m["id"] in used_ids]
        # Placeholder §0; corrected by the re-splice below once evidence exists,
        # which also avoids an extra transcript pass here.
        page = assemble_page(
            str(decl.get("title", slug)),
            "(refreshing status…)",
            banner,
            clean_why["why"],
            clean_sections,
            used_menu,
        )
        atlas_checkpoint.atomic_write(page_path, page)

    # Outside the lock (clear_live re-acquires it; record/index just touch files).
    errors, warnings = atlas_validate.validate(repo_root, slug)
    atlas_evidence.record(repo_root, slug, now)
    try:
        index = atlas_index.build_index(repo_root, None, now)
        (repo_root / "atlas" / "index.md").write_text(index + "\n", encoding="utf-8")
    except Exception:
        pass

    # Re-splice §0 now that evidence exists, so the just-written page is not
    # labelled "never generated" and the state token is bolded like a checkpoint.
    with (lock_dir / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur = page_path.read_text(encoding="utf-8")
        status_line = atlas_status.build_status_line(repo_root, slug, now)
        spliced = atlas_checkpoint.splice_status(cur, status_line)
        if spliced is not None:
            atlas_checkpoint.atomic_write(page_path, spliced)

    atlas_checkpoint.clear_live(repo_root, slug, now)
    atlas_checkpoint.log_event(
        repo_root,
        slug,
        {
            "ts": round(now, 3),
            "reason": "full_generate",
            "tier": "full",
            "tokens": tokens,
            "cost_usd": result.get("cost_usd"),
            "validate_errors": len(errors),
        },
    )
    return {
        "slug": slug,
        "generated": True,
        "words": len(page.split()),
        "citations": len(used_menu),
        "validate_errors": errors,
        "validate_warnings": warnings,
        "tokens": tokens,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Background full-page Atlas generation."
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument(
        "--force", action="store_true", help="ignore the live_model opt-in"
    )
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()
    result = generate(repo_root, args.slug, now, args.force)
    print(f"atlas_generate: {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
