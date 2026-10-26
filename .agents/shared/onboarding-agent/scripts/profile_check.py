#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check the onboarding agent's profile file against the rules in references/profile-file.md.

The profile is a short portrait in prose, so the checks are about its edges, not its shape:

- It starts with the ``# About you`` heading.
- Its last line is a ``Where this came from:`` line naming its sources.
- It has at most ``MAX_IDEAS`` bullets under an optional ``## Ideas to build`` heading, the personalized
  list of things worth building that later agents start from.
- It is at most ``MAX_WORDS`` words.
- It says what the person is and does, never what they are not: it holds none of the contrast phrasings the
  checker lists ("rather than", "not just", "instead of", "you've never", "hasn't written", "isn't any one", "not X but Y", and similar).
- It holds no email address, phone number, street address, or something that looks like a credential,
  or a word that points at one. Links and the date the file was written are not read as content.

It exits 0 and prints ``ok`` when the file passes, and exits 1 with one problem per line on stderr
otherwise. It checks form, not meaning: whether the portrait is about a third party or a sensitive topic
is the agent's judgement, and the denylist here only catches the plain cases.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_WORDS = 500
MAX_IDEAS = 8
IDEAS_HEADING = "## Ideas to build"
HEADING = "# About you"
SOURCES_PREFIX = "Where this came from:"

_URL = re.compile(r"https?://\S+")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CONTRAST = re.compile(
    r"\b(?:rather than|not just|not only|instead of|"
    r"(?:you|you've|you're|you have)\s+(?:never|haven't|don't|aren't|not)\b|"
    r"(?:hasn't|haven't|never)\s+(?:written|coded|programmed)|"
    r"no\s+(?:code|coding)\s+(?:background|experience)|"
    r"(?:isn't|aren't|wasn't|is not)\s+(?:any|your|something|about|the)\b|"
    r"\bnot\s+(?:\w+\s+){1,3}but\b)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Seven or more digits once separators are ignored, as in 555-123-4567 or +1 (555) 123 4567.
_PHONE = re.compile(r"(?:\+?\d[\s().-]?){7,}")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z0-9.'-]+\s+){1,3}(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct)\b",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_CREDENTIAL_WORDS = re.compile(
    r"\b(?:password|passcode|api[ _-]?key|secret|token)\b", re.IGNORECASE
)


def _idea_count(lines: list[str]) -> int:
    """How many bullets sit under the ``## Ideas to build`` heading, up to the next heading or the sources line."""
    count = 0
    in_ideas = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            in_ideas = stripped == IDEAS_HEADING
        elif stripped.startswith(SOURCES_PREFIX):
            in_ideas = False
        elif in_ideas and stripped.startswith(("- ", "* ")):
            count += 1
    return count


def check_profile(text: str) -> list[str]:
    """Every problem in ``text``, one string each; empty when the profile passes."""
    problems: list[str] = []
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    if first.strip() != HEADING:
        problems.append(f"The file must start with '{HEADING}'")
    last = next((line for line in reversed(lines) if line.strip()), "")
    if not last.strip().startswith(SOURCES_PREFIX):
        problems.append(
            f"The last line must be a '{SOURCES_PREFIX}' line naming its sources"
        )
    ideas = _idea_count(lines)
    if ideas > MAX_IDEAS:
        problems.append(f"{ideas} ideas, more than the {MAX_IDEAS} allowed")
    content = _ISO_DATE.sub("", _URL.sub("", text))
    words = len(content.split())
    if words > MAX_WORDS:
        problems.append(f"{words} words, more than the {MAX_WORDS} allowed")
    contrast = _CONTRAST.search(content)
    if contrast:
        problems.append(
            f"say what the person is and does, not what they are not: rewrite '{contrast.group(0)}' in positive words"
        )
    if _EMAIL.search(content):
        problems.append("the profile must not hold an email address")
    if _PHONE.search(content):
        problems.append("the profile must not hold a phone number")
    if _STREET.search(content):
        problems.append("the profile must not hold a street address")
    if _TOKEN.search(content) or _CREDENTIAL_WORDS.search(content):
        problems.append("the profile must not hold a credential or point at one")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a profile file against the onboarding rules."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="The profile file, e.g. data/profile/about-you.md, or - to read a draft from standard input",
    )
    args = parser.parse_args(argv)
    if str(args.path) == "-":
        text = sys.stdin.read()
    else:
        try:
            text = args.path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read {args.path}: {exc}", file=sys.stderr)
            return 1
    problems = check_profile(text)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
