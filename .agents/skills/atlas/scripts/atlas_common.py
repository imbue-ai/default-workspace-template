#!/usr/bin/env python3
"""Shared helpers for the Atlas skill scripts.

The scripts are standalone (run as `python3 <script>`) and import each other by
filename via the `sys.path.insert(0, <scripts dir>)` pattern each already carries.
This module holds the small pieces that were copy-pasted across many of them --
repo-root resolution, the topic-declaration loader, model-JSON parsing, the
page/state paths, the atomic write, and the page-section extractor -- so there is
one correct implementation of each.

It imports only the standard library, so any sibling can import it without a
module-load cycle.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path


def resolve_repo_root(arg: str | None) -> Path:
    """The repo root: an explicit --repo-root, else the git toplevel, else cwd.

    The canonical resolver (checks the git subprocess returncode and falls back to
    the current working directory) that ~14 scripts each open-coded.
    """
    if arg:
        return Path(arg).resolve()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(top.stdout.strip()) if top.returncode == 0 else Path.cwd()


def load_declaration(repo_root: Path, slug: str, *, missing_ok: bool = True) -> dict:
    """Load a topic declaration `atlas/topics/<slug>.toml`.

    `missing_ok=True` (default): return `{}` when the file is missing or malformed
    -- the {}-returning behavior atlas_transcript and the index use.
    `missing_ok=False`: raise the underlying `OSError` (missing) or
    `tomllib.TOMLDecodeError` (malformed) -- the raising behavior the generate and
    live-refresh workers rely on. atlas_status wraps these in its own
    `DeclarationError`.
    """
    path = repo_root / "atlas" / "topics" / f"{slug}.toml"
    if not missing_ok:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def parse_model_json(text: str) -> dict:
    """Parse a JSON object out of a model reply; `{}` unless it is an object.

    Tolerates a ```-fenced reply and, failing a whole-string parse, recovers the
    first `{...}` span. A dict guard keeps a non-object reply (the model answers
    `"none"`, a list, a number) from reaching `.get()`/`.items()` and crashing a
    caller. This is the single strong parser the generate, detect, and live-refresh
    workers all share.
    """
    import re

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def page_path(repo_root: Path, slug: str) -> Path:
    """The topic's one-pager markdown: `atlas/<slug>.md`."""
    return repo_root / "atlas" / f"{slug}.md"


def state_dir(repo_root: Path, slug: str) -> Path:
    """The topic's machine-state directory: `data/.state/atlas/<slug>`."""
    return repo_root / "data" / ".state" / "atlas" / slug


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace, so a mid-write kill (a hook's
    `timeout 8`) can never leave a page or state file truncated."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def tokens_last_hour(repo_root: Path, slug: str, now: float, reason: str) -> int:
    """Output tokens logged for `reason` on this topic in the last hour.

    Shared by the full-generation (`reason="full_generate"`) and live-refresh
    (`reason="live_refresh"`) workers, which each bound their own hourly spend.
    """
    log = state_dir(repo_root, slug) / "checkpoints.jsonl"
    if not log.is_file():
        return 0
    total = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("reason") == reason and (now - float(e.get("ts", 0))) < 3600:
            total += int(e.get("tokens", 0) or 0)
    return total


def _is_section_boundary(line: str) -> bool:
    """A page section ends at the next `## ` heading OR the Evidence block /
    footnote definitions / horizontal rule that follow the last section under no
    heading. Including `---` is what keeps a §7 replace from clobbering the tail."""
    return (
        line.startswith("## ")
        or line.startswith("<details>")
        or line.startswith("[^")
        or line.strip() == "---"
    )


def section_end(lines: list[str], start: int) -> int:
    """Index of the first boundary line after `start`, or len(lines)."""
    return next(
        (j for j in range(start + 1, len(lines)) if _is_section_boundary(lines[j])),
        len(lines),
    )


def section_body(text: str, header: str) -> str:
    """The body between a `## header` line and the next section boundary.

    One implementation so the generate and live-refresh workers extract section
    bodies identically (they previously disagreed on whether `---` is a boundary).
    """
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return ""
    return "\n".join(lines[start + 1 : section_end(lines, start)]).strip()
