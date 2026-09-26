#!/usr/bin/env python3
"""Move this workspace's mngr pin, together with the build plumbing a private pin needs.

``pyproject.toml``'s ``[tool.uv.sources]`` pins every mngr package to one repo at one
commit: the public mirror for every release, or the private ``mngr-internal`` repo
while a branch iterates on a paired mngr change. A build from the private repo needs a
credential, which docker builds receive as a BuildKit secret: a ``--secret`` build
argument on every docker create template in ``.mngr/settings.toml`` and a
``--mount=type=secret`` on the two Dockerfile ``RUN`` lines that fetch mngr. Those
lines make the build require BuildKit (``docker buildx``), which a public pin must not
do -- Ubuntu's ``docker.io`` and Homebrew's docker CLI ship without it -- so they exist
only while the pin is private. This script is the one writer of all of it: a pin move
adds or removes the BuildKit lines with the repo, and ``--check`` refuses a tree where
the two disagree.

    set_mngr_pin.py --public <commit>      # pin the public mirror, drop the BuildKit lines
    set_mngr_pin.py --internal <commit>    # pin mngr-internal, add the BuildKit lines
    set_mngr_pin.py --check                # print the pin kind; fail if the tree is inconsistent
    set_mngr_pin.py --require-public       # --check, and fail if the pin is private

The mngr repo's ``scripts/bump_dwt_mngr_pin.py`` calls the first two from a checkout of
both repos; this repo's CI and release gate run the last two.
"""

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from list_mngr_plugins import (
    INTERNAL_KIND,
    INTERNAL_MNGR_REPO,
    PUBLIC_KIND,
    PUBLIC_MNGR_REPO,
    PYPROJECT_PATH,
    MngrPinError,
    read_mngr_source,
)

MNGR_REPOS = (PUBLIC_MNGR_REPO, INTERNAL_MNGR_REPO)
SETTINGS_PATH = ".mngr/settings.toml"
DOCKERFILE_PATH = "system/Dockerfile"
BUILDKIT_SECRET_ID = "mngr_internal_git_token"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class BuildKitLines:
    """One file's BuildKit text: what it reads with a public pin and with a private one."""

    path: str
    public: str
    internal: str

    def count(self, text: str) -> tuple[int, int]:
        """``(public occurrences, internal occurrences)`` in ``text``.

        The public text may be a prefix of the internal one, in which case every internal
        occurrence also matches it and is discounted.
        """
        internal = text.count(self.internal)
        public = text.count(self.public) - (
            internal if self.public in self.internal else 0
        )
        return public, internal

    def to_public(self, text: str) -> str:
        return text.replace(self.internal, self.public)

    def to_internal(self, text: str) -> str:
        return self.to_public(text).replace(self.public, self.internal)


_DOCKERFILE_ARG = '"--file=system/Dockerfile", '
_SETTINGS_LINES = BuildKitLines(
    path=SETTINGS_PATH,
    public=_DOCKERFILE_ARG,
    internal=f'{_DOCKERFILE_ARG}"--secret=id={BUILDKIT_SECRET_ID},env=MNGR_INTERNAL_GIT_TOKEN", ',
)
# The two RUN lines that fetch mngr from the pin: the dependency pre-warm and the
# workspace build.
_DOCKERFILE_RUN_COMMANDS = (
    "chmod +x /usr/local/bin/default-workspace-template-install-dependencies"
    " && default-workspace-template-install-dependencies",
    "bash /home/user/workspace/system/scripts/build_workspace.sh",
)
_DOCKERFILE_LINES = tuple(
    BuildKitLines(
        path=DOCKERFILE_PATH,
        public=f"RUN {command}",
        internal=f"RUN --mount=type=secret,id={BUILDKIT_SECRET_ID},required=false \\\n    {command}",
    )
    for command in _DOCKERFILE_RUN_COMMANDS
)
BUILDKIT_LINES = (_SETTINGS_LINES, *_DOCKERFILE_LINES)


class MngrPinTreeError(Exception):
    """The tree's pin and its BuildKit lines cannot be read as one consistent state."""


def buildkit_lines_kind(files: dict[str, str]) -> str:
    """Which pin kind the BuildKit lines of ``files`` (path -> text) are shaped for.

    Every occurrence in every file must agree, and each file must carry at least one;
    anything else is an inconsistent tree, reported with what was found where.
    """
    kinds: set[str] = set()
    problems: list[str] = []
    for lines in BUILDKIT_LINES:
        public, internal = lines.count(files[lines.path])
        if public and internal:
            problems.append(
                f"{lines.path} mixes {public} public and {internal} private-pin BuildKit line(s)"
            )
        elif public:
            kinds.add(PUBLIC_KIND)
        elif internal:
            kinds.add(INTERNAL_KIND)
        else:
            problems.append(
                f"{lines.path} has none of the lines this script maintains: {lines.public!r}"
            )
    if len(kinds) > 1:
        problems.append("the files disagree on whether the pin is public or private")
    if problems:
        raise MngrPinTreeError("; ".join(problems))
    return kinds.pop()


def rewrite_pin_sources(pyproject_text: str, repo_url: str, rev: str) -> str:
    """Point every ``[tool.uv.sources]`` entry on an mngr repo at ``repo_url`` and ``rev``."""
    sources = (
        tomllib.loads(pyproject_text).get("tool", {}).get("uv", {}).get("sources", {})
    )
    pinned = [
        name
        for name, source in sources.items()
        if isinstance(source, dict) and source.get("git") in MNGR_REPOS
    ]
    if not pinned:
        raise MngrPinError(
            f"{PYPROJECT_PATH} pins nothing to {' or '.join(MNGR_REPOS)}"
        )
    repos_pattern = "|".join(re.escape(repo) for repo in MNGR_REPOS)
    text = pyproject_text
    for name in pinned:
        text, count = re.subn(
            rf'^({re.escape(name)} = \{{ git = ")(?:{repos_pattern})(", rev = ")[0-9a-f]{{40}}(")',
            rf"\g<1>{repo_url}\g<2>{rev}\g<3>",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise MngrPinError(
                f"could not rewrite the source for {name} in {PYPROJECT_PATH}"
            )
    return text


def set_pin(root: Path, kind: str, rev: str) -> list[str]:
    """Pin ``root`` to ``rev`` on the repo ``kind`` names and shape its BuildKit lines to match.

    Returns the paths written. Refuses a tree whose BuildKit lines are mixed rather than
    guessing which state to move from; lines that merely disagree with the pyproject's
    repo are brought into line, since that is the repair.
    """
    if not _FULL_SHA.match(rev):
        raise MngrPinError(f"pin a full 40-hex commit, not {rev!r}")
    repo_url = INTERNAL_MNGR_REPO if kind == INTERNAL_KIND else PUBLIC_MNGR_REPO
    files = {lines.path: (root / lines.path).read_text() for lines in BUILDKIT_LINES}
    buildkit_lines_kind(files)
    written = [PYPROJECT_PATH]
    (root / PYPROJECT_PATH).write_text(
        rewrite_pin_sources((root / PYPROJECT_PATH).read_text(), repo_url, rev)
    )
    for lines in BUILDKIT_LINES:
        text = (
            lines.to_internal(files[lines.path])
            if kind == INTERNAL_KIND
            else lines.to_public(files[lines.path])
        )
        if text != files[lines.path]:
            (root / lines.path).write_text(text)
            files[lines.path] = text
            if lines.path not in written:
                written.append(lines.path)
    return written


def check_tree(root: Path) -> str:
    """The pin kind of the tree at ``root``; raises when its BuildKit lines say otherwise."""
    pin_kind = read_mngr_source((root / PYPROJECT_PATH).read_text()).kind
    lines_kind = buildkit_lines_kind(
        {lines.path: (root / lines.path).read_text() for lines in BUILDKIT_LINES}
    )
    if pin_kind != lines_kind:
        raise MngrPinTreeError(
            f"{PYPROJECT_PATH} pins the {pin_kind} mngr repo but the BuildKit lines are shaped for a "
            f"{lines_kind} pin; run set_mngr_pin.py --{pin_kind} <commit> to make them agree"
        )
    return pin_kind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--public", metavar="COMMIT", help=f"pin {PUBLIC_MNGR_REPO} at COMMIT"
    )
    what.add_argument(
        "--internal", metavar="COMMIT", help=f"pin {INTERNAL_MNGR_REPO} at COMMIT"
    )
    what.add_argument(
        "--check",
        action="store_true",
        help="print the pin kind; fail if the tree is inconsistent",
    )
    what.add_argument(
        "--require-public",
        action="store_true",
        help="like --check, and fail if the pin names the private repo",
    )
    parser.add_argument(
        "--repo-root", default=".", help="the workspace root (default: cwd)"
    )
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    try:
        if args.check or args.require_public:
            kind = check_tree(root)
            if args.require_public and kind != PUBLIC_KIND:
                print(
                    f"the template pins mngr from {INTERNAL_MNGR_REPO}, which only a build holding a credential "
                    "can fetch; this pin never merges to main or ships (see docs/system/workspace-internals.md)",
                    file=sys.stderr,
                )
                return 1
            print(kind)
        elif args.internal:
            for path in set_pin(root, INTERNAL_KIND, args.internal):
                print(path)
        else:
            for path in set_pin(root, PUBLIC_KIND, args.public):
                print(path)
    except (MngrPinError, MngrPinTreeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
