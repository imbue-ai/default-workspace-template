#!/usr/bin/env python3
"""Automatic feature detection -- the self-filling half of the book.

Features are still declared, never silently inferred: this proposes a feature
(`status = "proposed"`) that a human ratifies (decision 3). The mechanism is the
"mechanical trigger, model confirms" shape (option C):

  1. **Heuristic gate (cheap, no model):** has the agent done enough new work
     since the last detection pass? (assistant turns >= threshold). If not, exit.
  2. **Model confirm + draft (one cheap call):** hand the reduced recent
     transcript and the existing projects+features to the model and ask whether a
     *new, substantial feature* is present that the existing features do not cover.
     A feature belongs to a project -- an existing one (reuse its slug) or a new
     one -- so a big project accumulates several feature pages. The model dedups
     by feature and returns {project, slug, title, why}.
  3. If proposed, write `atlas/topics/<slug>.toml` (status=proposed, project set,
     this agent recorded in `agent_ids`) and a skeleton page to fill.

Never fires autonomously in a loop: run it from the low-frequency backstop or by
hand. Exit 0 always.

Usage:
    atlas_detect.py [--repo-root R] [--min-turns N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_ai  # noqa: E402
import atlas_common  # noqa: E402
import atlas_config  # noqa: E402
import atlas_transcript  # noqa: E402

DEFAULT_MIN_TURNS = 15

SYSTEM = (
    "You organize an AI agent's worklog into a two-level book: PROJECTS (a whole "
    "app or effort) each containing FEATURE one-pagers (a large, distinct piece of "
    "that project's work). You are strict: a feature is a substantial, multi-step "
    "effort a developer would catch up on -- not a one-off question or small tweak. "
    "A big project has SEVERAL feature pages; a wholly separate effort is a new "
    "project. Never duplicate an existing feature."
)

PROMPT_TEMPLATE = """\
Existing projects and their feature pages (do NOT duplicate any feature below;
reuse an existing project slug when the new work belongs to it):
{existing}

Recent agent work (most recent transcript turns):
{recent}

Is there ONE new, substantial FEATURE in this recent work not already covered by
a feature above? It may belong to an existing project (reuse its slug) or start a
new project. Respond with STRICT JSON, no prose:
  {{"new_feature": true, "project": "existing-or-new-project-slug",
    "slug": "kebab-case-feature-slug", "title": "Short feature title",
    "why": "1-2 sentences: the problem this feature's work addresses"}}
or, if there is nothing new and substantial enough:
  {{"new_feature": false}}
"""


def _state_path(repo_root: Path) -> Path:
    return repo_root / "data" / ".state" / "atlas" / "_detect.json"


def _read_detect_state(repo_root: Path) -> dict:
    p = _state_path(repo_root)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_detect_state(repo_root: Path, state: dict) -> None:
    p = _state_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def existing_topics(repo_root: Path) -> list[dict]:
    out = []
    topics_dir = repo_root / "atlas" / "topics"
    for path in sorted(topics_dir.glob("*.toml")):
        if path.stem.startswith("_"):
            continue
        try:
            with path.open("rb") as fh:
                decl = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError):
            continue
        slug = decl.get("slug", path.stem)
        out.append(
            {
                "slug": slug,
                "title": decl.get("title", ""),
                "project": str(decl.get("project") or slug),
            }
        )
    return out


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model response, tolerating fences.

    Thin wrapper over the shared parser so `atlas_detect.parse_json` (used by the
    tests and by atlas_route) keeps working.
    """
    return atlas_common.parse_model_json(text)


def format_existing_menu(existing: list[dict]) -> str:
    """Render the existing feature list grouped by project, for a model prompt.

    Shared by the sweep detector and the prompt router so the model sees the same
    project->feature menu (and reuses existing project slugs) in both.
    """
    by_project: dict[str, list[dict]] = {}
    for t in existing:
        by_project.setdefault(t["project"], []).append(t)
    return (
        "\n".join(
            f"- project {proj}:\n"
            + "\n".join(f"    - {t['slug']}: {t['title']}" for t in feats)
            for proj, feats in by_project.items()
        )
        or "(none yet)"
    )


def valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug or ""))


def _toml_str(value: str) -> str:
    """Escape a free-text value for a TOML basic string (quotes/backslashes/newlines)."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\r", " ").replace("\n", " ")
    return value.strip()


def write_proposal(
    repo_root: Path,
    project: str,
    slug: str,
    title: str,
    why: str,
    now: float,
    live_model: bool = False,
) -> Path:
    started = time.strftime("%Y-%m-%d", time.localtime(now))
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    decl = repo_root / "atlas" / "topics" / f"{slug}.toml"
    decl.parent.mkdir(parents=True, exist_ok=True)
    agent_line = f'agent_ids = ["{agent_id}"]\n' if agent_id else ""
    # live_model on a proposal lets the checkpoint clock keep §1/§7 fresh during
    # the task and run the end-of-task full generation (A1), so the auto-created
    # page fills itself rather than staying a skeleton until a human runs /atlas.
    live_line = (
        "live_model = true   # auto-refresh + end-of-task full generation\n"
        if live_model
        else ""
    )
    decl.write_text(
        f'slug = "{slug}"\n'
        f'title = "{_toml_str(title)}"\n'
        f'project = "{project}"   # groups this feature under a project (tab)\n'
        f'status = "proposed"   # auto-detected; a human ratifies (decision 3)\n'
        f'started = "{started}"\n'
        f'checkpoint_interval = "5m"\n'
        f"{live_line}\n"
        f"[match]\n{agent_line}",
        encoding="utf-8",
    )
    page = repo_root / "atlas" / f"{slug}.md"
    if not page.is_file():
        page.write_text(
            f"# {title}\n\n"
            f"> **Unconfirmed** — auto-created from recent work; not yet confirmed "
            f"as a tracked topic.\n\n"
            f"<!-- atlas:status -->\nPROPOSED · pending first write-up\n<!-- /atlas:status -->\n\n"
            f"## Current state\n\n(This page fills in as the work continues.)\n\n"
            f"## Why this exists\n\n{why}\n\n"
            f"## How it got here\n\n(Pending.)\n\n"
            f"## Decisions\n\n(Pending.)\n\n"
            f"## Implementation shape\n\n(Pending.)\n\n"
            f"## Open questions\n\n(Pending.)\n\n"
            f"## Next steps\n\n- (Pending — this page fills in as the work continues.)\n",
            encoding="utf-8",
        )
    return decl


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Detect and propose new Atlas topics.")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--min-turns", type=int, default=DEFAULT_MIN_TURNS)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="never write; print the verdict"
    )
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()

    # Detection is part of the one-pager book; skip when that toggle is off.
    if not atlas_config.is_enabled(repo_root, "pages"):
        return 0

    # The current agent's own work is the source (label-less detection scope).
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if not agent_id:
        print("atlas_detect: no current agent; nothing to scan", file=sys.stderr)
        return 0
    paths = atlas_transcript.transcript_paths([agent_id])

    detect_state = _read_detect_state(repo_root)
    last_ts = float(detect_state.get("last_ts") or 0.0)

    # 1. Heuristic gate -- enough new work to bother the model?
    act = atlas_transcript.activity_since(paths, last_ts)
    if act["turns"] < args.min_turns:
        print(
            f"atlas_detect: {act['turns']} turns since last pass (< {args.min_turns}); skip"
        )
        return 0

    recent = atlas_transcript.reduce(paths, last_ts, max_chars=8000)
    existing = existing_topics(repo_root)
    existing_str = format_existing_menu(existing)
    prompt = PROMPT_TEMPLATE.format(existing=existing_str, recent=recent)

    # 2. Model confirm + draft.
    try:
        result = atlas_ai.complete(prompt, system=SYSTEM)
    except atlas_ai.AIUnavailable as exc:
        print(f"atlas_detect: model unavailable ({exc}); skip", file=sys.stderr)
        return 0
    verdict = parse_json(result["text"])
    print(f"atlas_detect: verdict={json.dumps(verdict)} cost={result.get('cost_usd')}")

    if not args.dry_run:
        detect_state["last_ts"] = now
        _write_detect_state(repo_root, detect_state)

    if not verdict.get("new_feature"):
        return 0
    slug = str(verdict.get("slug", "")).strip()
    project = str(verdict.get("project", "")).strip() or slug
    if not valid_slug(slug) or not valid_slug(project):
        print(
            f"atlas_detect: model returned an invalid slug/project ({project!r}/{slug!r}); skip",
            file=sys.stderr,
        )
        return 0
    # Dedup by FEATURE slug only -- a project may hold several distinct features.
    if any(t["slug"] == slug for t in existing):
        print(f"atlas_detect: feature {slug} already exists; skip", file=sys.stderr)
        return 0
    if args.dry_run:
        print(
            f"atlas_detect: would propose feature '{slug}' under project '{project}': {verdict.get('title')}"
        )
        return 0

    decl = write_proposal(
        repo_root,
        project,
        slug,
        str(verdict.get("title", slug)),
        str(verdict.get("why", "")),
        now,
    )
    print(
        f"atlas_detect: proposed feature '{slug}' under project '{project}' -> {decl}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
