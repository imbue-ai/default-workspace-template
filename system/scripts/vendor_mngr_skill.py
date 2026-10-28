#!/usr/bin/env python3
"""Vendor the published Imbue Studio skill from the pinned mngr commit, or check the vendored copy.

``.agents/skills/imbue-studio/SKILL.md`` is a verbatim copy of
``apps/minds/skills_plugin/skills/imbue-studio/SKILL.md`` in the mngr repository, kept in
this tree so every harness that reads the skills tree has it. Like the BuildKit lines
``set_mngr_pin.py`` moves, it is coupled to the mngr pin: this script is its one writer,
run by the mngr repo's ``scripts/bump_dwt_mngr_pin.py`` right after every pin move, and
its one checker, run by CI's ``pin-is-public`` job.

    vendor_mngr_skill.py --write --mngr-repo <checkout> --rev <commit>   # copy the skill at COMMIT
    vendor_mngr_skill.py --check                                          # compare against the pinned commit on GitHub
    vendor_mngr_skill.py --check --mngr-repo <checkout>                   # compare against a local checkout instead

``--write`` takes the commit explicitly because the pin may name a public export the
local checkout does not hold; ``--check`` reads the pin from ``pyproject.toml``. A private
pin is fetched with the ``MNGR_INTERNAL_GIT_TOKEN`` credential when the environment holds
one; without it, pass ``--mngr-repo``.
"""

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from list_mngr_plugins import PYPROJECT_PATH, GitPin, MngrPinError, read_mngr_source

SKILL_SOURCE_PATH = "apps/minds/skills_plugin/skills/imbue-studio/SKILL.md"
SKILL_VENDORED_PATH = ".agents/skills/imbue-studio/SKILL.md"
INTERNAL_GIT_TOKEN_ENV_VAR = "MNGR_INTERNAL_GIT_TOKEN"
_FETCH_TIMEOUT_SECONDS = 60
_GIT_TIMEOUT_SECONDS = 60

# Reads the skill's text at one commit of the pinned repo; injectable so the comparison is testable offline.
SkillReader = Callable[[GitPin], str]


class VendoredSkillError(Exception):
    """The vendored skill could not be read, written, or does not match the pin."""


def raw_file_url(pin: GitPin) -> str:
    """The GitHub raw URL of the skill at the pinned commit."""
    return f"{pin.git_url.replace('https://github.com/', 'https://raw.githubusercontent.com/')}/{pin.rev}/{SKILL_SOURCE_PATH}"


def read_skill_from_github(pin: GitPin) -> str:
    """Fetch the skill at the pinned commit; a private pin needs the token in the environment."""
    request = urllib.request.Request(raw_file_url(pin))
    token = os.environ.get(INTERNAL_GIT_TOKEN_ENV_VAR, "")
    if pin.is_internal:
        if not token:
            raise VendoredSkillError(
                f"the pin names the private repo and {INTERNAL_GIT_TOKEN_ENV_VAR} is unset; "
                "pass --mngr-repo <checkout> to compare locally"
            )
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=_FETCH_TIMEOUT_SECONDS
        ) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise VendoredSkillError(
            f"could not fetch {SKILL_SOURCE_PATH} at {pin.rev[:12]}: {exc}"
        ) from exc


def checkout_skill_reader(mngr_repo: Path) -> SkillReader:
    """A reader that takes the skill from a local mngr checkout at the pinned commit."""

    def read(pin: GitPin) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(mngr_repo), "show", f"{pin.rev}:{SKILL_SOURCE_PATH}"],
                check=True,
                capture_output=True,
                encoding="utf-8",
                timeout=_GIT_TIMEOUT_SECONDS,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise VendoredSkillError(
                f"could not read {SKILL_SOURCE_PATH} at {pin.rev[:12]} from {mngr_repo}: {exc}"
            ) from exc

    return read


def write_vendored_skill(root: Path, pin: GitPin, read_skill: SkillReader) -> bool:
    """Copy the skill at ``pin`` over the vendored copy; returns whether the copy changed."""
    text = read_skill(pin)
    vendored = root / SKILL_VENDORED_PATH
    if vendored.is_file() and vendored.read_text(encoding="utf-8") == text:
        return False
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text(text, encoding="utf-8")
    return True


def check_vendored_skill(root: Path, read_skill: SkillReader) -> None:
    """Raise VendoredSkillError unless the vendored copy equals the skill at the pinned commit."""
    pin = read_mngr_source((root / PYPROJECT_PATH).read_text())
    vendored = root / SKILL_VENDORED_PATH
    if not vendored.is_file():
        raise VendoredSkillError(f"{SKILL_VENDORED_PATH} is missing")
    if vendored.read_text(encoding="utf-8") != read_skill(pin):
        raise VendoredSkillError(
            f"{SKILL_VENDORED_PATH} differs from {SKILL_SOURCE_PATH} at the pinned mngr commit {pin.rev[:12]}; "
            "re-run the pin move in the mngr repo (just dwt-mngr-pin*), which vendors the skill, rather than editing it here"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--write",
        action="store_true",
        help="copy the skill at --rev from --mngr-repo over the vendored copy",
    )
    what.add_argument(
        "--check",
        action="store_true",
        help="fail unless the vendored copy matches the pinned commit",
    )
    parser.add_argument(
        "--repo-root", default=".", help="the workspace root (default: cwd)"
    )
    parser.add_argument(
        "--mngr-repo",
        help="a local mngr checkout to read the skill from: required with --write; with --check, compared against instead of GitHub",
    )
    parser.add_argument(
        "--rev", help="the mngr commit to copy the skill from (required with --write)"
    )
    args = parser.parse_args(argv)
    if args.rev and not args.write:
        parser.error(
            "--rev only applies to --write; --check compares against the pinned commit"
        )
    root = Path(args.repo_root).resolve()
    try:
        if args.write:
            if not args.mngr_repo or not args.rev:
                parser.error("--write needs --mngr-repo and --rev")
            pin = read_mngr_source((root / PYPROJECT_PATH).read_text())
            local_pin = GitPin(git_url=pin.git_url, rev=args.rev)
            if write_vendored_skill(
                root,
                local_pin,
                checkout_skill_reader(Path(args.mngr_repo).resolve()),
            ):
                print(SKILL_VENDORED_PATH)
        else:
            read_skill = (
                checkout_skill_reader(Path(args.mngr_repo).resolve())
                if args.mngr_repo
                else read_skill_from_github
            )
            check_vendored_skill(root, read_skill)
            print(f"{SKILL_VENDORED_PATH} matches the pinned mngr commit")
    except (MngrPinError, VendoredSkillError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
