#!/usr/bin/env python3
"""Mechanical validation for an Atlas page -- the checklist as code.

Replaces the by-hand checklist so a generated page can't quietly drift. Errors
fail (exit 1); warnings are advisory (exit 0). Run at generation time and on the
scheduled sweep.

Checks (errors): the eight sections present and in order; §0 status markers
present and balanced; body within the 1,100-word cap; every citation marker
resolves to a footnote definition; pin markers balanced; no secret-scanner hits.
Checks (warnings): date-like tokens in §1; future modals in §3; unused footnote
definitions.

Usage:
    atlas_validate.py <slug> [--repo-root R]
Exit: 0 if no errors (warnings allowed), 1 if any error, 2 if the page is missing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402

REQUIRED_SECTIONS = [
    "## Current state",
    "## Why this exists",
    "## How it got here",
    "## Decisions",
    "## Implementation shape",
    "## Open questions",
    "## Next steps",
]

# Basic secret patterns for the pre-write gate (transcripts can leak secrets and
# pages are committed). Not a substitute for a full scanner, but fails closed on
# the obvious cases; gitleaks is used instead when available.
SECRET_PATTERNS = [
    (r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----", "private key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"(?i)bearer\s+[A-Za-z0-9._\-]{24,}", "bearer token"),
    (
        r"(?i)(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
        "hardcoded credential",
    ),
]

FUTURE_MODALS = ("we will", "the plan is", "plan to", "going to", "will be added")


def section_span(text: str, header: str) -> tuple[int, int] | None:
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return None
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
        len(lines),
    )
    return (start, end)


def secret_hits(text: str) -> list[str]:
    hits = _gitleaks_hits(text)
    if hits is not None:  # gitleaks ran and produced a parseable report
        return hits
    # No gitleaks, or it errored -- decide by its report contents, never by its
    # exit code (gitleaks exits 1 for BOTH leaks-found and runtime errors, so an
    # error would otherwise be misreported as a secret). Fall back to the regex.
    return [label for pattern, label in SECRET_PATTERNS if re.search(pattern, text)]


def _gitleaks_hits(text: str) -> list[str] | None:
    """Run gitleaks and read its JSON report; None if unavailable/unparseable.

    Decides by the findings array, not the exit code. Returns [] for a clean
    scan, a list of rule ids for real findings, or None to signal "fall back to
    the regex gate" (gitleaks missing, crashed, or wrote no usable report).
    """
    gl = _which("gitleaks")
    if not gl:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="atlas_secretscan_") as d:
            src = Path(d) / "page.md"
            src.write_text(text, encoding="utf-8")
            report = Path(d) / "report.json"
            subprocess.run(
                [
                    gl,
                    "detect",
                    "--no-git",
                    "--source",
                    str(src),
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report),
                ],
                capture_output=True,
                text=True,
            )
            if not report.is_file():
                return None  # gitleaks didn't produce a report -> it errored
            findings = json.loads(report.read_text(encoding="utf-8") or "[]")
            if not isinstance(findings, list):
                return None
            return [f"gitleaks: {f.get('RuleID', 'secret')}" for f in findings]
    except (OSError, json.JSONDecodeError):
        return None


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def validate(repo_root: Path, slug: str) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    page = atlas_common.page_path(repo_root, slug)
    if not page.is_file():
        return ([f"page missing: {page}"], [])
    text = page.read_text(encoding="utf-8")

    # Sections present and in order.
    positions = []
    for header in REQUIRED_SECTIONS:
        span = section_span(text, header)
        if span is None:
            errors.append(f"missing section: {header}")
        else:
            positions.append((header, span[0]))
    ordered = [h for h, _ in sorted(positions, key=lambda x: x[1])]
    present_required = [h for h in REQUIRED_SECTIONS if h in ordered]
    if ordered != present_required:
        errors.append(f"sections out of order: {ordered}")

    # §0 status markers present and balanced.
    if (
        text.count("<!-- atlas:status -->") != 1
        or text.count("<!-- /atlas:status -->") != 1
    ):
        errors.append("§0 status markers missing or unbalanced")

    # Body word cap (everything before the collapsed Evidence block / footnotes).
    body = text.split("<details>")[0]
    body = re.split(r"\n\[\^", body)[0]
    cur = section_span(body, "## Current state")
    body_from_s1 = "\n".join(body.split("\n")[cur[0] :]) if cur else body
    words = len(body_from_s1.split())
    if words > 1100:
        errors.append(f"over word cap: {words} > 1100 -- split the topic")

    # Citation markers must resolve to a footnote definition.
    used = set(re.findall(r"\[\^([A-Za-z0-9_-]+)\](?!:)", text))
    defined = set(re.findall(r"^\[\^([A-Za-z0-9_-]+)\]:", text, re.MULTILINE))
    for missing in sorted(used - defined):
        errors.append(f"unresolved citation: [^{missing}]")
    for unused in sorted(defined - used):
        warnings.append(f"unused footnote: [^{unused}]")

    # Pin markers balanced.
    if text.count("<!-- atlas:pinned") != text.count("<!-- /atlas:pinned -->"):
        errors.append("unbalanced <!-- atlas:pinned --> markers")

    # Secret gate.
    for hit in secret_hits(text):
        errors.append(f"possible secret in page: {hit}")

    # Warnings: dates in §1, future modals in §3.
    s1 = section_span(text, "## Current state")
    if s1:
        s1_text = "\n".join(text.split("\n")[s1[0] + 1 : s1[1]])
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b20\d{2}\b", s1_text):
            warnings.append("§1 (current state) contains a date-like token")
    s3 = section_span(text, "## How it got here")
    if s3:
        s3_text = "\n".join(text.split("\n")[s3[0] + 1 : s3[1]]).lower()
        for modal in FUTURE_MODALS:
            if modal in s3_text:
                warnings.append(f"§3 (history) contains a future modal: '{modal}'")
                break
    return (errors, warnings)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate an Atlas page.")
    parser.add_argument("slug")
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)

    errors, warnings = validate(repo_root, args.slug)
    if (
        not errors
        and not warnings
        and atlas_common.page_path(repo_root, args.slug).is_file()
    ):
        pass  # nothing to report
    for w in warnings:
        print(f"WARN  {args.slug}: {w}")
    for e in errors:
        print(f"ERROR {args.slug}: {e}")
    if errors and errors[0].startswith("page missing"):
        return 2
    if errors:
        print(f"atlas_validate: {args.slug} FAILED ({len(errors)} error(s))")
        return 1
    print(f"atlas_validate: {args.slug} OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
