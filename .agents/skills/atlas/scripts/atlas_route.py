#!/usr/bin/env python3
"""Prompt-driven task routing -- classify an incoming task to a page.

Fired (detached) from the UserPromptSubmit hook. Given the just-submitted user
prompt, it decides whether the task:

  - **belongs to an existing feature** -> associate the current agent with that
    slug (so this session's work tracks under the right page), or
  - **is a new, substantial feature** -> auto-create a `proposed` page (slug
    derived by the model, never asked of the user), so tracking starts from
    turn one, or
  - **is too small to track** -> do nothing.

Only "large" complexity creates a page: a multi-step effort a developer would
later catch up on, not a one-off tweak. Auto-created pages start `status =
"proposed"` (a human ratifies or drops them) -- the same shape as sweep
detection, reusing `atlas_detect.write_proposal`.

Guardrails, so this stays cheap despite firing per prompt:
  - **Heuristic gate:** skip short/trivial prompts before any model call.
  - **Debounce:** skip a repeat of the last prompt and bursts within a short
    window.
  - **Cheap model** (Haiku), one call.

Always exits 0 -- routing must never block or fail a prompt.

Usage:
    atlas_route.py [--input HOOK_JSON_FILE | --prompt TEXT] [--repo-root R]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_ai  # noqa: E402
import atlas_checkpoint  # noqa: E402
import atlas_common  # noqa: E402
import atlas_config  # noqa: E402
import atlas_detect  # noqa: E402
import atlas_transcript  # noqa: E402

# Payload temp files older than this are swept as stragglers (a router that died
# before unlinking its own).
STALE_PAYLOAD_S = 300

# Below this length a prompt is almost never a trackable feature request.
MIN_PROMPT_CHARS = 40
# Don't classify the same prompt twice, or fire twice within this window.
CLASSIFY_MIN_INTERVAL_S = 15
# Pure acknowledgements / continuations -- never a new task.
TRIVIAL_RE = re.compile(
    r"^(yes|no|ok(ay)?|sure|thanks?|thank you|continue|go( ahead)?|do it|"
    r"proceed|next|stop|yep|yeah|nope|good|great|perfect|done)[.!]?$",
    re.IGNORECASE,
)

SYSTEM = (
    "You route an incoming task to a two-level book of PROJECTS containing FEATURE "
    "one-pagers. FIRST try to match it to an EXISTING feature; if it belongs to one, "
    "route there REGARDLESS of size. Only if it fits no existing feature AND is a "
    "substantial (multi-step, multi-turn) NEW effort do you propose a new feature -- "
    "under an existing project when it belongs to one, else a new project. A task "
    "that fits no existing feature but is small is 'none' (do not create a page). "
    "Never duplicate an existing feature."
)

PROMPT_TEMPLATE = """\
Existing projects and their feature pages:
{existing}

The user's incoming task:
{task}

Classify it. Respond with STRICT JSON, no prose:
  {{"route": "existing", "slug": "<one of the existing feature slugs>"}}
or
  {{"route": "new", "project": "existing-or-new-project-slug",
    "slug": "kebab-case-feature-slug", "title": "Short feature title",
    "why": "1-2 sentences: the problem this feature addresses",
    "complexity": "large|medium|small"}}
or, if it is too small or does not describe trackable feature work:
  {{"route": "none"}}
"""


def _route_state_path(repo_root: Path) -> Path:
    return repo_root / "data" / ".state" / "atlas" / "_route.json"


def _read_route_state(repo_root: Path) -> dict:
    p = _route_state_path(repo_root)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_route_state(repo_root: Path, state: dict) -> None:
    p = _route_state_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    atlas_checkpoint.atomic_write(p, json.dumps(state, indent=2))


def _reserve_slot(repo_root: Path, prompt_hash: str, now: float) -> bool:
    """Under a lock, decide whether to classify this prompt and reserve the slot.

    Reserving (writing last_hash/last_ts) BEFORE the slow model call means a
    concurrent router -- another prompt, or another agent sharing this workspace,
    each firing its own detached router at the shared `_route.json` -- sees the
    updated state and debounces, instead of both spending a call and both creating
    a page for the same task.
    """
    lock_dir = repo_root / "data" / ".state" / "atlas"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "route.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read_route_state(repo_root)
        if state.get("last_hash") == prompt_hash:
            return False  # already classified this exact prompt
        if (now - float(state.get("last_ts") or 0.0)) < CLASSIFY_MIN_INTERVAL_S:
            return False  # debounce a burst of prompts
        state["last_hash"] = prompt_hash
        state["last_ts"] = now
        _write_route_state(repo_root, state)
        return True


def _sweep_stale_payloads(repo_root: Path, now: float) -> None:
    """Remove orphaned route-payload.* files (a router that died before unlink)."""
    for p in (repo_root / "data" / ".state" / "atlas").glob("route-payload.*"):
        try:
            if now - p.stat().st_mtime > STALE_PAYLOAD_S:
                p.unlink()
        except OSError:
            pass


def _prompt_from_input(text: str) -> str:
    """Extract the user prompt from a hook JSON payload, or treat text as the prompt."""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        return str(payload.get("prompt") or payload.get("user_prompt") or "").strip()
    return text


def is_trivial(prompt: str) -> bool:
    """True if the prompt is too short or a pure acknowledgement to bother the model."""
    stripped = prompt.strip()
    if len(stripped) < MIN_PROMPT_CHARS:
        return True
    return bool(TRIVIAL_RE.match(stripped))


def classify(repo_root: Path, prompt: str) -> dict:
    """One cheap model call. Returns the parsed verdict (empty dict on failure)."""
    existing = atlas_detect.existing_topics(repo_root)
    existing_str = atlas_detect.format_existing_menu(existing)
    task = prompt if len(prompt) <= 2000 else prompt[:2000] + " …"
    prompt_text = PROMPT_TEMPLATE.format(existing=existing_str, task=task)
    result = atlas_ai.complete(prompt_text, system=SYSTEM)
    return atlas_detect.parse_json(result["text"])


def apply_verdict(repo_root: Path, verdict: dict, now: float) -> dict:
    """Act on a routing verdict. Returns a result dict describing what happened."""
    route = str(verdict.get("route", "none")).strip().lower()
    existing = atlas_detect.existing_topics(repo_root)
    existing_slugs = {t["slug"] for t in existing}

    if route == "existing":
        slug = str(verdict.get("slug", "")).strip()
        if slug not in existing_slugs:
            return {"action": "skip", "reason": f"unknown existing slug {slug!r}"}
        # Associate this session with the routed page and make it the active topic,
        # so only THIS page absorbs the agent's work (any size).
        recorded = atlas_transcript.record_current_agent(repo_root, slug)
        _mark_active(repo_root, slug)
        return {"action": "associate", "slug": slug, "agent": recorded}

    if route == "new":
        if str(verdict.get("complexity", "")).strip().lower() != "large":
            return {"action": "skip", "reason": "not large enough for a page"}
        slug = str(verdict.get("slug", "")).strip()
        project = str(verdict.get("project", "")).strip() or slug
        if not atlas_detect.valid_slug(slug) or not atlas_detect.valid_slug(project):
            return {
                "action": "skip",
                "reason": f"invalid slug/project {project!r}/{slug!r}",
            }
        if slug in existing_slugs:
            return {"action": "skip", "reason": f"feature {slug} already exists"}
        decl = atlas_detect.write_proposal(
            repo_root,
            project,
            slug,
            str(verdict.get("title", slug)),
            str(verdict.get("why", "")),
            now,
            live_model=True,
        )
        _mark_active(repo_root, slug)
        return {"action": "create", "slug": slug, "project": project, "decl": str(decl)}

    return {"action": "skip", "reason": "route=none"}


def _mark_active(repo_root: Path, slug: str) -> None:
    """Link the routed topic to this task: make it the agent's active topic (so
    attribution follows it) and flag it for a full write-up at task end (so a task
    linked to a page regenerates it regardless of how many turns it took)."""
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if agent_id:
        atlas_checkpoint.set_active_topic(repo_root, agent_id, slug)
    atlas_checkpoint.mark_generate_pending(repo_root, slug)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Route an incoming task to an Atlas page."
    )
    parser.add_argument("--input", default=None, help="hook JSON payload file")
    parser.add_argument("--prompt", default=None, help="the prompt text directly")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument(
        "--force", action="store_true", help="ignore debounce/heuristic gate"
    )
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    now = args.now if args.now is not None else time.time()

    # Clean up any orphaned payload files (a prior router that died before unlink).
    _sweep_stale_payloads(repo_root, now)

    if args.prompt is not None:
        prompt = args.prompt.strip()
    elif args.input:
        try:
            prompt = _prompt_from_input(Path(args.input).read_text(encoding="utf-8"))
        except OSError:
            return 0
        finally:
            # The wrapper hands us a throwaway file for the payload; drop it once
            # read so the detached worker leaves nothing behind.
            Path(args.input).unlink(missing_ok=True)
    else:
        prompt = _prompt_from_input(sys.stdin.read())

    # Routing/auto-pages are part of the book; skip when that toggle is off (after
    # the payload file has been cleaned up above).
    if not atlas_config.is_enabled(repo_root, "pages"):
        return 0

    if not prompt:
        return 0
    if not args.force and is_trivial(prompt):
        return 0

    prompt_hash = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16]
    # Reserve the slot under a lock BEFORE the (slow) model call, so concurrent
    # routers debounce instead of double-classifying/double-creating.
    if not args.force and not _reserve_slot(repo_root, prompt_hash, now):
        return 0

    try:
        verdict = classify(repo_root, prompt)
    except atlas_ai.AIUnavailable as exc:
        print(f"atlas_route: model unavailable ({exc}); skip", file=sys.stderr)
        return 0

    result = apply_verdict(repo_root, verdict, now)
    # Observability: a durable trace on the affected page's own event log, so a
    # user can see why a page appeared or got associated (the stdout below is
    # discarded by the detached hook).
    if result.get("action") in {"create", "associate"} and result.get("slug"):
        atlas_checkpoint.log_event(
            repo_root,
            result["slug"],
            {
                "ts": round(now, 3),
                "reason": "route",
                "action": result["action"],
                "route": verdict.get("route"),
            },
        )
    print(f"atlas_route: verdict={json.dumps(verdict)} -> {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
