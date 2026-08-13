#!/usr/bin/env python3
"""The Atlas checkpoint clock -- the deterministic half of phase 2.

Fired from a rate-limited PostToolUse hook (a natural pause between actions) and
from the Stop hook at turn end. On the interval it refreshes the §0 status line
in place -- which is free, no model -- and records a checkpoint event so cost and
movement can be measured (decision 6). It never calls a model and never commits.

The richer §1/§7 refresh is *not* done here: a hook cannot run a model
(decision 1). When the agent has done work -- assistant turns in its transcript,
not git commits -- since the last live refresh, this engine raises a
`live_pending` flag; the working agent picks it up at a free moment and runs
`/atlas <slug> --live` in its own context. The token-spending out-of-band worker
that would do that on the clock is the next increment, sized from the movement
data this engine logs.

Fast path: if the interval has not elapsed, a PostToolUse call returns in a few
file stats and writes nothing. Everything runs under a per-slug flock so the
engine, the generating agent, and any other agents on the topic never race.

Usage:
    atlas_checkpoint.py --reason {posttooluse|turn_end|manual} [--slug S]
                        [--repo-root R] [--now EPOCH]

Always exits 0 -- a checkpoint must never block the agent that triggered it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

# Reuse the status-line logic and the transcript reader (the agent's own work).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402
import atlas_config  # noqa: E402
import atlas_evidence  # noqa: E402
import atlas_status  # noqa: E402
import atlas_transcript  # noqa: E402

MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 600
DEFAULT_INTERVAL_S = 300  # "ordinary implementation work" -- the 3-5m default
# Turns of work since the last full generation that mark a task "large" -- the
# end-of-task full-generation trigger (A1). Override per topic with full_gen_turns.
LARGE_TASK_TURNS = 12


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def atomic_write(path: Path, text: str) -> None:
    """Atomic write (temp file + os.replace). Kept here so callers and tests can
    keep using `atlas_checkpoint.atomic_write`; the implementation is shared."""
    atlas_common.atomic_write(path, text)


def parse_interval(value: str | None) -> int:
    """Parse a checkpoint_interval ('3m', '90s', '2') to clamped seconds."""
    if not value:
        return DEFAULT_INTERVAL_S
    text = str(value).strip().lower()
    try:
        if text.endswith("s"):
            seconds = int(float(text[:-1]))
        elif text.endswith("m"):
            seconds = int(float(text[:-1]) * 60)
        else:
            seconds = int(float(text) * 60)  # bare number = minutes
    except ValueError:
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, seconds))


def shape_default_interval(repo_root: Path, slug: str, now: float) -> int:
    """Pick a default interval from the topic's recent *work* rate.

    Fast-moving work (many turns/hour) checkpoints tighter; a quiet or
    long-grinding topic relaxes. Measured from the agent's transcript, not git,
    and only used when the declaration does not set checkpoint_interval.
    """
    paths = atlas_transcript.transcript_paths(
        atlas_transcript.resolve_agent_ids(repo_root, slug)
    )
    kw = atlas_transcript.topic_keywords(repo_root, slug)  # scope to this topic
    recent = atlas_transcript.activity_since(paths, now - 3600, kw)["turns"]
    if recent >= 20:
        return 120  # 2m -- high turn rate
    if recent == 0:
        return 600  # 10m -- quiet / long grinding steps
    return DEFAULT_INTERVAL_S  # 5m -- ordinary


def fullgen_threshold(decl: dict) -> int:
    """Turns-since-last-full-generation that mark a task 'large', from the decl.

    Tolerates a bad `full_gen_turns` (0 = 'always'; garbage -> the default)
    rather than letting int() raise inside the checkpoint and abort the whole
    fire, including the free §0 refresh.
    """
    raw = decl.get("full_gen_turns")
    if raw is None:
        return LARGE_TASK_TURNS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return LARGE_TASK_TURNS


def fullgen_due(
    route_pending: bool, work_since_gen: int, threshold: int, debounced: bool
) -> bool:
    """Whether to spawn an end-of-task full generation.

    Fires when the router linked this task to the topic (`route_pending` -- any
    size) or enough work has accrued since the last full generation, and not
    within the debounce window.
    """
    return debounced and (route_pending or work_since_gen >= threshold)


def current_branch(repo_root: Path) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)


def state_dir(repo_root: Path, slug: str) -> Path:
    """The topic's state dir. Kept here so callers and the viewer can keep using
    `atlas_checkpoint.state_dir`; the implementation is shared."""
    return atlas_common.state_dir(repo_root, slug)


def read_state(repo_root: Path, slug: str) -> dict:
    path = state_dir(repo_root, slug) / "state.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(repo_root: Path, slug: str, state: dict) -> None:
    d = state_dir(repo_root, slug)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write(d / "state.json", json.dumps(state, indent=2))
    # Keep the plain last_checkpoint marker the status script reads, in sync.
    if "last_checkpoint" in state:
        atomic_write(d / "last_checkpoint", str(state["last_checkpoint"]))


def log_event(repo_root: Path, slug: str, event: dict) -> None:
    d = state_dir(repo_root, slug)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "checkpoints.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def _active_path(repo_root: Path) -> Path:
    return repo_root / "data" / ".state" / "atlas" / "active.json"


def active_topic(repo_root: Path, agent_id: str) -> str | None:
    """The topic an agent is currently working, as set by the prompt router."""
    p = _active_path(repo_root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get(agent_id) if isinstance(data, dict) else None


def set_active_topic(repo_root: Path, agent_id: str, slug: str) -> None:
    """Record the topic an agent is currently working (the router calls this).

    Attribution keys off this: a topic an agent is *associated* with but not
    *actively working* does not absorb that agent's turns, so an unrelated task
    can't update the wrong one-pager.
    """
    p = _active_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[agent_id] = slug
    atomic_write(p, json.dumps(data, indent=2))


def mark_generate_pending(repo_root: Path, slug: str) -> None:
    """Flag a topic to get a full write-up at the next turn end (the router calls
    this when it links a task to the topic), so the page regenerates regardless of
    how many turns the task took. Locked, so it doesn't race the checkpoint clock.
    """
    lock_dir = state_dir(repo_root, slug)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(repo_root, slug)
        state["route_pending"] = True
        write_state(repo_root, slug, state)


def _suppressed_by_active(
    repo_root: Path, slug: str, decl: dict, agents: list[str]
) -> bool:
    """True if this topic shares an agent but isn't that agent's active topic.

    Only applies to topics explicitly associated by `match.agent_ids` (the case
    that cross-attributes). If none of those agents has an active topic set yet,
    fall back to the legacy keyword scoping (return False).
    """
    explicit = (decl.get("match", {}) or {}).get("agent_ids") or []
    if not explicit:
        return False
    actives = {active_topic(repo_root, str(a)) for a in explicit}
    actives.discard(None)
    if not actives:
        return False
    return slug not in actives


def splice_status(page_text: str, status_line: str) -> str | None:
    """Replace the text between the §0 status markers. None if markers absent."""
    start = "<!-- atlas:status -->"
    end = "<!-- /atlas:status -->"
    if start not in page_text or end not in page_text:
        return None
    head, _, rest = page_text.partition(start)
    _old, _, tail = rest.partition(end)
    # Bold the leading state token for readability; the rest stays plain.
    parts = status_line.split(" · ", 1)
    formatted = f"**{parts[0]}**" + (f" · {parts[1]}" if len(parts) > 1 else "")
    return f"{head}{start}\n{formatted}\n{end}{tail}"


def process_topic(repo_root: Path, slug: str, reason: str, now: float) -> dict:
    """Run one checkpoint for one topic. Returns a result dict (also logged)."""
    try:
        decl = atlas_status.load_declaration(repo_root, slug)
    except atlas_status.DeclarationError as exc:
        return {
            "slug": slug,
            "fired": False,
            "reason": "no_declaration",
            "detail": str(exc),
        }

    # Fast path uses a CHEAP interval: the declared value, or the shaped value
    # cached from the last real fire (default until one happens). This keeps an
    # ordinary tool call to a few file stats -- shape_default_interval scans the
    # transcript, so it is recomputed only when we actually fire (inside the lock).
    state = read_state(repo_root, slug)
    declared = decl.get("checkpoint_interval")
    if declared:
        interval_s = parse_interval(declared)
    else:
        interval_s = int(state.get("shaped_interval_s") or DEFAULT_INTERVAL_S)
    last_cp = state.get("last_checkpoint")
    elapsed = (now - last_cp) if last_cp else None

    # Fast path: an ordinary tool call before the interval elapses does nothing.
    if reason == "posttooluse" and elapsed is not None and elapsed < interval_s:
        return {"slug": slug, "fired": False, "reason": "not_elapsed"}

    lock_path = state_dir(repo_root, slug)
    lock_path.mkdir(parents=True, exist_ok=True)
    with (lock_path / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        # Re-read state under the lock -- the authoritative copy for the movement
        # check, the spawn debounce, and the write (the pre-lock read above was
        # only for the cheap gate and may be stale vs a concurrent fire).
        state = read_state(repo_root, slug)
        last_cp = state.get("last_checkpoint")

        # Recompute the shaped interval only on a real fire, and cache it for the
        # next fast-path gate.
        if not declared:
            interval_s = shape_default_interval(repo_root, slug, now)
            state["shaped_interval_s"] = interval_s

        # Movement is the agent's own work since the last rich refresh -- assistant
        # turns in the transcript, NOT git commits. A long, uncommitted work
        # session still marks the live tier stale.
        agents = atlas_transcript.resolve_agent_ids(repo_root, slug)
        paths = atlas_transcript.transcript_paths(agents)
        kw = atlas_transcript.topic_keywords(repo_root, slug)  # scope to this topic
        # A topic an agent is associated with but not *actively working* must not
        # absorb that agent's turns -- otherwise an unrelated task updates the
        # wrong one-pager.
        suppressed = _suppressed_by_active(repo_root, slug, decl, agents)
        live = state.get("live", {}) or {}
        last_refresh = live.get("last_refresh")
        if last_refresh is None:
            # First sighting: establish the baseline, don't count history as movement.
            live["last_refresh"] = now
            turns_since_live = tokens_since_live = 0
        elif suppressed:
            turns_since_live = tokens_since_live = 0
        else:
            act = atlas_transcript.activity_since(paths, last_refresh, kw)
            turns_since_live = act["turns"]
            tokens_since_live = act["tokens"]
        moved = turns_since_live > 0

        live_pending = bool(live.get("pending")) or moved
        live["pending"] = live_pending

        # End-of-task full generation (A1): at turn_end on a live topic, spawn the
        # background full-page generator when EITHER the router linked this task to
        # the topic (`route_pending` -- regenerate regardless of size) OR enough
        # work has accrued since the last full generation (>= threshold). Gated
        # behind live_pending-or-route_pending so an idle turn_end pays for no extra
        # transcript pass.
        should_fullgen = False
        route_pending = bool(state.get("route_pending"))
        if (
            reason == "turn_end"
            and bool(decl.get("live_model"))
            and not suppressed
            and (live_pending or route_pending)
        ):
            fullgen = state.get("fullgen", {}) or {}
            threshold = fullgen_threshold(decl)
            ev = atlas_evidence.check(repo_root, slug, max(threshold, 1))
            work_since_gen = (
                ev["new_turns"]
                if ev.get("new_turns") is not None
                else atlas_transcript.activity_since(paths, 0.0, kw)["turns"]
            )
            debounced = (now - float(fullgen.get("spawn_ts") or 0.0)) > interval_s
            if fullgen_due(route_pending, work_since_gen, threshold, debounced):
                should_fullgen = True
                fullgen["spawn_ts"] = now
                state["fullgen"] = fullgen
                if route_pending:
                    state["route_pending"] = False  # consumed this task's link

        # Debounce the live-worker spawn: opted in, pending, not spawned within the
        # last interval, and NOT superseded by a full generation this fire -- so a
        # skipped/no-op full-gen decision does not burn the live debounce, and the
        # turn_end fire right after a posttooluse fire can't double-launch.
        should_spawn = (
            not should_fullgen
            and not suppressed
            and live_pending
            and bool(decl.get("live_model"))
            and (now - float(live.get("spawn_ts") or 0.0)) > interval_s
        )
        if should_spawn:
            live["spawn_ts"] = now

        # Turns since the last §0 checkpoint, for the status line's movement figure.
        turns_since_cp = (
            atlas_transcript.activity_since(paths, last_cp, kw)["turns"]
            if last_cp
            else 0
        )

        # Record the checkpoint BEFORE building §0, so the status line reflects
        # this checkpoint ("checkpointed just now") rather than the previous one.
        state["last_checkpoint"] = now
        state["live"] = live
        write_state(repo_root, slug, state)

        # §0 refresh -- free, always done on a fire.
        status_line = atlas_status.build_status_line(repo_root, slug, now)
        page_path = atlas_common.page_path(repo_root, slug)
        spliced = False
        if page_path.is_file():
            new_text = splice_status(page_path.read_text(encoding="utf-8"), status_line)
            if new_text is not None:
                atomic_write(page_path, new_text)
                spliced = True

        event = {
            "ts": round(now, 3),
            "reason": reason,
            "tier": "status",
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "interval_s": interval_s,
            "status_spliced": spliced,
            "turns_since_checkpoint": turns_since_cp,
            "turns_since_live": turns_since_live,
            "tokens_since_live": tokens_since_live,
            "live_pending": live_pending,
            "fullgen_spawned": should_fullgen,
            "tokens": 0,  # §0 costs no model; live-tier tokens logged when it runs
        }
        log_event(repo_root, slug, event)

    # Opt-in out-of-band model work, detached so the hook stays non-blocking.
    # A large task ending triggers a full page (§1-§7); otherwise, if the live
    # tier moved, the narrow §1/§7 refresh. Both debounced above.
    if should_fullgen:
        _spawn_worker(repo_root, slug, "atlas_generate.py")
    elif should_spawn:
        _spawn_worker(repo_root, slug, "atlas_live_refresh.py")

    return {"slug": slug, "fired": True, "reason": reason, **event}


def _spawn_worker(repo_root: Path, slug: str, script_name: str) -> None:
    """Fire-and-forget an out-of-band worker; never block or raise into the hook."""
    script = Path(__file__).resolve().parent / script_name
    try:
        subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [
                sys.executable,
                str(script),
                "--slug",
                slug,
                "--repo-root",
                str(repo_root),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def target_slugs(
    repo_root: Path, slug: str | None, all_topics: bool = False
) -> list[str]:
    if slug:
        return [slug]
    topics_dir = repo_root / "atlas" / "topics"
    if not topics_dir.is_dir():
        return []
    branch = current_branch(repo_root)
    current_agent = os.environ.get("MNGR_AGENT_ID")
    slugs: list[str] = []
    for path in sorted(topics_dir.glob("*.toml")):
        try:
            decl = atlas_status.load_declaration(repo_root, path.stem)
        except atlas_status.DeclarationError:
            continue
        if decl.get("status") in {"shipped", "abandoned"}:
            continue  # done topics do not checkpoint
        # The idle backstop (--all) sweeps every live topic regardless of branch.
        if all_topics:
            slugs.append(path.stem)
            continue
        # The hook path touches topics matching the current branch OR associated
        # with the current agent -- auto-created/tracked topics carry agent_ids and
        # no branch, so without the agent check the turn-end hook would never
        # process them and their pages would never generate. (Non-active topics are
        # then suppressed inside process_topic, so only the routed one updates.)
        match = decl.get("match", {}) or {}
        patterns = match.get("branches", [])
        if branch and any(fnmatch(branch, pat) for pat in patterns):
            slugs.append(path.stem)
            continue
        agent_ids = match.get("agent_ids") or []
        if current_agent and current_agent in agent_ids:
            slugs.append(path.stem)
    return slugs


def pending_slugs(repo_root: Path, slug: str | None) -> list[str]:
    """Topics (branch-scoped, or one) whose live tier has agent work waiting."""
    out = []
    for s in target_slugs(repo_root, slug):
        state = read_state(repo_root, s)
        if (state.get("live") or {}).get("pending"):
            out.append(s)
    return out


def clear_live(repo_root: Path, slug: str, now: float) -> dict:
    """Mark the live tier freshly refreshed: clear the flag, reset the baseline.

    Called by the skill after the working agent regenerates §1/§7 via --live, so
    the next checkpoint measures movement from this point.
    """
    lock_path = state_dir(repo_root, slug)
    lock_path.mkdir(parents=True, exist_ok=True)
    with (lock_path / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(repo_root, slug)
        # Reset the movement baseline to now: transcript turns after this instant
        # are what will mark the live tier stale again.
        state["live"] = {"pending": False, "last_refresh": now}
        write_state(repo_root, slug, state)
    log_event(
        repo_root,
        slug,
        {
            "ts": round(now, 3),
            "reason": "live_refresh",
            "tier": "live",
            "live_pending": False,
        },
    )
    return {"slug": slug, "cleared": True}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the Atlas checkpoint clock.")
    parser.add_argument(
        "--reason", default="manual", choices=["posttooluse", "turn_end", "manual"]
    )
    parser.add_argument(
        "--slug", default=None, help="one topic; default: all on this branch"
    )
    parser.add_argument("--repo-root", default=None)
    parser.add_argument(
        "--now", type=float, default=None, help="epoch override (testing)"
    )
    parser.add_argument(
        "--reminder",
        action="store_true",
        help="print a one-line nudge if any topic's live tier is stale, then exit",
    )
    parser.add_argument(
        "--clear-live",
        action="store_true",
        help="mark the live tier refreshed (call after a --live regeneration)",
    )
    parser.add_argument(
        "--all",
        dest="all_topics",
        action="store_true",
        help="process every live topic regardless of branch (the idle backstop)",
    )
    args = parser.parse_args(argv)

    repo_root = atlas_common.resolve_repo_root(args.repo_root)

    now = args.now if args.now is not None else time.time()

    # The one-pager book is toggleable; when off, the checkpoint clock (§0,
    # reminders, page/live spawns) does nothing. The in-chat summary is separate.
    if not atlas_config.is_enabled(repo_root, "pages"):
        return 0

    if args.reminder:
        pending = pending_slugs(repo_root, args.slug)
        if pending:
            print(
                f"[atlas] Live tier stale (new work since last refresh) for: "
                f"{', '.join(pending)}. At a convenient pause, run "
                f"`/atlas <slug> --live` to refresh current-state and next-steps."
            )
        return 0

    if args.clear_live:
        for slug in target_slugs(repo_root, args.slug):
            clear_live(repo_root, slug, now)
        return 0

    results = []
    for slug in target_slugs(repo_root, args.slug, all_topics=args.all_topics):
        try:
            results.append(process_topic(repo_root, slug, args.reason, now))
        except Exception as exc:  # never let a checkpoint break the agent
            print(f"atlas_checkpoint: {slug}: {exc}", file=sys.stderr)

    fired = [r for r in results if r.get("fired")]
    if fired:
        for r in fired:
            print(
                f"atlas: checkpointed {r['slug']} ({r['reason']}) -- "
                f"live_pending={r.get('live_pending')}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
