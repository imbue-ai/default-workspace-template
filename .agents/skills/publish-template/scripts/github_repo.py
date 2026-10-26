#!/usr/bin/env python3
"""Create a template's GitHub repo, and tag it once it is published, through latchkey.

    python3 github_repo.py create --name <repo> --description <text> \\
        --visibility private|public --worktree <WT>
    python3 github_repo.py topic --repo <owner>/<repo>

`create` does everything that has to happen between "the repo name is confirmed"
and the push:

  1. creates the repo (`POST /user/repos`), its description suffixed with the
     flow-version marker every published template carries;
  2. closes every surface where a non-collaborator could add content (pull
     requests restricted to collaborators; issues, wiki, projects and discussions
     off). A failure here is reported as a follow-up, not a failed publish: the
     repo already exists and is private unless the user chose otherwise;
  3. replaces the README's repo-URL placeholder with `<owner>/<repo>` in the
     worktree and commits it, so the push carries a working Open in Imbue Studio
     button and never needs a fix-up commit after it.

It prints `<owner>/<repo>`. `topic` sets the `minds-template` topic, which is what
makes templates discoverable as a group; run it after the push succeeds.

Every request body is built as JSON here, so a description with quotes in it
cannot break the call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from validate_template import REPO_URL_PLACEHOLDER

_API = "https://api.github.com"
_FLOW_VERSION_SUFFIX = " (minds template v2)"
_TEMPLATE_TOPIC = "minds-template"
_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_LOCKDOWN_SETTINGS = {
    "pull_request_creation_policy": "collaborators_only",
    "has_issues": False,
    "has_wiki": False,
    "has_projects": False,
    "has_discussions": False,
}


class GitHubRepoError(Exception):
    """Raised when GitHub, or latchkey in front of it, refuses a request."""


def _request(method: str, url: str, body: dict[str, object]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "latchkey",
                "curl",
                "-s",
                "-X",
                method,
                url,
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(body),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise GitHubRepoError(f"cannot run latchkey: {e}") from e
    if completed.returncode != 0:
        raise GitHubRepoError(
            f"{method} {url} failed (curl exit {completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise GitHubRepoError(
            f"{method} {url} answered: {completed.stdout[:500]}"
        ) from e
    if not isinstance(response, dict):
        raise GitHubRepoError(f"{method} {url} answered: {completed.stdout[:500]}")
    return response


def _explain(response: dict[str, Any]) -> str:
    text = json.dumps(response)
    if "not permitted" in text:
        return (
            f"{text} -- the github-rest-api grant (github-read-user + github-write-all) "
            "is missing or too narrow; request it again"
        )
    if "already exists" in text:
        return f"{text} -- the name is taken on this account; ask the user for another"
    return text


def create_repo(name: str, description: str, is_private: bool) -> str:
    """Create the repo and return its `<owner>/<repo>`."""
    response = _request(
        "POST",
        f"{_API}/user/repos",
        {
            "name": name,
            "description": description + _FLOW_VERSION_SUFFIX,
            "private": is_private,
        },
    )
    owner = response.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(login, str) or response.get("name") != name:
        raise GitHubRepoError(f"the repo was not created: {_explain(response)}")
    return f"{login}/{name}"


def lock_down(repo: str) -> str | None:
    """Close the collaboration surface; the problem if it did not take, else None."""
    try:
        response = _request("PATCH", f"{_API}/repos/{repo}", dict(_LOCKDOWN_SETTINGS))
    except GitHubRepoError as e:
        return str(e)
    if "name" not in response:
        return _explain(response)
    return None


def point_readme_at(worktree: Path, repo: str) -> bool:
    """Replace the README's repo-URL placeholder and commit it; whether anything changed."""
    readme = worktree / "README.md"
    if not readme.is_file():
        return False
    text = readme.read_text(encoding="utf-8")
    if REPO_URL_PLACEHOLDER not in text:
        return False
    readme.write_text(text.replace(REPO_URL_PLACEHOLDER, repo), encoding="utf-8")
    for command in (
        ["git", "add", "README.md"],
        [
            "git",
            "commit",
            "-q",
            "-m",
            "readme: point the Open in Imbue Studio link at the published repo",
        ],
    ):
        subprocess.run(
            command, cwd=worktree, check=True, capture_output=True, text=True
        )
    return True


def set_template_topic(repo: str) -> None:
    response = _request(
        "PUT", f"{_API}/repos/{repo}/topics", {"names": [_TEMPLATE_TOPIC]}
    )
    names = response.get("names")
    if not isinstance(names, list) or _TEMPLATE_TOPIC not in names:
        raise GitHubRepoError(f"the topic was not set: {_explain(response)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a template's GitHub repo, or tag a published one."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--description", required=True)
    create.add_argument("--visibility", required=True, choices=("private", "public"))
    create.add_argument("--worktree", required=True, type=Path)
    topic = subcommands.add_parser("topic")
    topic.add_argument("--repo", required=True, help="<owner>/<repo>")
    args = parser.parse_args(argv)

    try:
        if args.command == "topic":
            set_template_topic(args.repo)
            print(f"github_repo: tagged {args.repo} {_TEMPLATE_TOPIC}", file=sys.stderr)
            return 0
        if not _REPO_NAME_PATTERN.fullmatch(args.name):
            print(
                f"github_repo: {args.name!r} is not a valid repo name "
                "(letters, digits, '.', '_', '-'; not starting with '-')",
                file=sys.stderr,
            )
            return 2
        repo = create_repo(args.name, args.description, args.visibility == "private")
    except GitHubRepoError as e:
        print(f"github_repo: {e}", file=sys.stderr)
        return 1

    lockdown_problem = lock_down(repo)
    if lockdown_problem is not None:
        print(
            f"github_repo: follow-up: {repo} was created, but closing its issues, wiki, "
            f"projects, discussions and outside pull requests did not take: {lockdown_problem}",
            file=sys.stderr,
        )
    try:
        is_readme_pointed = point_readme_at(args.worktree, repo)
    except subprocess.CalledProcessError as e:
        print(repo)
        print(
            f"github_repo: {repo} was created, but committing README.md in {args.worktree} "
            f"failed: {(e.stderr or '').strip()}. Commit README.md there by hand, then go on "
            "to the push -- never re-run create, the repo already exists.",
            file=sys.stderr,
        )
        return 1
    if not is_readme_pointed:
        print(
            f"github_repo: follow-up: README.md in {args.worktree} is missing or carries "
            f"no {REPO_URL_PLACEHOLDER}, so its Open in Imbue Studio link was not pointed "
            f"at {repo}; check it and commit a fix before the push.",
            file=sys.stderr,
        )
    print(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
