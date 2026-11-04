#!/usr/bin/env python3
"""Create a site's GitHub repo, push the site, and turn GitHub Pages on, all through latchkey.

    uv run --no-sync github_pages.py whoami
    uv run --no-sync github_pages.py create-repo --name <repo> --description <text> --owner <username>
    uv run --no-sync github_pages.py push --site <SITE> --repo <owner>/<repo> --message <text>
    uv run --no-sync github_pages.py enable --repo <owner>/<repo>
    uv run --no-sync github_pages.py wait --repo <owner>/<repo> [--timeout-seconds 600]
    uv run --no-sync github_pages.py disable --repo <owner>/<repo>
    uv run --no-sync github_pages.py delete-repo --repo <owner>/<repo> --yes-delete-forever

`whoami` prints the connected account's username. `create-repo` makes a PUBLIC
repo (GitHub Pages on a private repo needs a paid plan) with issues, wiki and
projects off, after checking the connected account is `--owner`, the username
the user gave; a different account exits 4 without creating anything, and a
taken name exits 3. It prints `<owner>/<repo>`.

`push` publishes the site folder's current files as one new commit on `main`:
it makes the folder its own git repo on first use (never part of the
workspace's), refuses to run unless `check_site.py lint` passes, and never
force-pushes. The commit is authored as the user's GitHub no-reply address, so
no email of theirs goes public. Prints the pushed commit.

`enable` turns Pages on from `main`'s root (already on is fine) and prints the
site URL; `wait` polls the latest Pages build until it is built (exit 0) or
errored (exit 1, with GitHub's message). `disable` turns Pages off and keeps the
repo; `delete-repo` deletes the repo, which cannot be undone.

The GitHub credential is injected by the latchkey gateway: no token is in a URL,
on disk, or in git config.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from publish_to_github_pages_skill.check_site import lint as lint_site

_API = "https://api.github.com"
_REPO_NAME = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_FULL_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class GitHubPagesError(Exception):
    """Raised when GitHub, latchkey, or git refuses a step."""


def api(
    method: str, path: str, body: dict[str, object] | None = None
) -> tuple[int, Any]:
    """One REST call through latchkey; returns (HTTP status, parsed JSON or None)."""
    command = [
        "latchkey",
        "curl",
        "-s",
        "-X",
        method,
        f"{_API}{path}",
        "-w",
        "\n%{http_code}",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    if body is not None:
        command += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as e:
        raise GitHubPagesError(f"cannot run latchkey: {e}") from e
    if completed.returncode != 0:
        raise GitHubPagesError(
            f"{method} {path} failed (curl exit {completed.returncode}): {completed.stderr.strip()}"
        )
    text, _, status = completed.stdout.rpartition("\n")
    try:
        code = int(status)
    except ValueError as e:
        raise GitHubPagesError(
            f"{method} {path} answered: {completed.stdout[:500]}"
        ) from e
    try:
        payload = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = text
    if "No credentials found" in json.dumps(payload):
        raise GitHubPagesError(
            "GitHub is not connected yet: file the two GitHub permission requests (the skill's step 6) "
            "and wait for both approvals"
        )
    if "not permitted" in json.dumps(payload):
        raise GitHubPagesError(
            f"{method} {path} was not permitted by latchkey: {json.dumps(payload)[:300]} -- this endpoint "
            "is outside the granted GitHub permissions (github-read-user, github-read-repos, github-write-all); "
            "check the grants before asking again"
        )
    return code, payload


def whoami() -> str:
    code, payload = api("GET", "/user")
    if code != 200 or not isinstance(payload, dict) or "login" not in payload:
        raise GitHubPagesError(
            f"GET /user answered {code}: {json.dumps(payload)[:300]}"
        )
    return str(payload["login"])


def create_repo(name: str, description: str, owner: str) -> str:
    if not _REPO_NAME.fullmatch(name):
        raise GitHubPagesError(
            f"{name!r} is not a valid repo name (letters, digits, '.', '_', '-')"
        )
    login = whoami()
    if login.lower() != owner.lower():
        print(
            f"github_pages: the connected GitHub account is {login!r}, not {owner!r}; ask the user which to use",
            file=sys.stderr,
        )
        raise SystemExit(4)
    homepage = f"https://{login.lower()}.github.io/{name}/"
    code, payload = api(
        "POST",
        "/user/repos",
        {
            "name": name,
            "description": description,
            "homepage": homepage,
            "private": False,
            "has_issues": False,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
        },
    )
    if code == 422 and "already exists" in json.dumps(payload):
        print(
            f"github_pages: {login}/{name} already exists; ask the user for another name",
            file=sys.stderr,
        )
        raise SystemExit(3)
    if code != 201 or not isinstance(payload, dict):
        raise GitHubPagesError(
            f"the repo was not created ({code}): {json.dumps(payload)[:300]}"
        )
    if payload.get("private") is not False:
        raise GitHubPagesError(
            f"the repo came back private; GitHub Pages needs it public: {payload.get('html_url')}"
        )
    return str(payload["full_name"])


def _git(site: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=site, capture_output=True, text=True, check=False, env=env
    )
    if completed.returncode != 0:
        raise GitHubPagesError(
            f"git {' '.join(args[:2])} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _ensure_own_repo(site: Path) -> None:
    """Make the site folder its own repo, and refuse if git would resolve to any other repo."""
    if not (site / ".git").exists():
        _git(site, "init", "-q", "-b", "main")
    toplevel = Path(_git(site, "rev-parse", "--show-toplevel")).resolve()
    if toplevel != site.resolve():
        raise GitHubPagesError(
            f"{site} resolves to the repo at {toplevel}, not its own; refusing to publish from it"
        )


def _gateway(repo: str) -> tuple[list[str], str]:
    gateway = os.environ.get("LATCHKEY_GATEWAY")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD")
    if not gateway or not password:
        raise GitHubPagesError(
            "LATCHKEY_GATEWAY and LATCHKEY_GATEWAY_PASSWORD must be set (they are in a workspace)"
        )
    options = ["-c", f"http.extraHeader=X-Latchkey-Gateway-Password: {password}"]
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")
    if override:
        options += [
            "-c",
            f"http.extraHeader=X-Latchkey-Gateway-Permissions-Override: {override}",
        ]
    return options, f"{gateway}/gateway/https://github.com/{repo}.git"


def push(site: Path, repo: str, message: str) -> str:
    if not _FULL_NAME.fullmatch(repo):
        raise GitHubPagesError(f"--repo {repo!r} must be <owner>/<repo>")
    if not site.is_dir():
        raise GitHubPagesError(f"{site} does not exist")
    # stdout carries only the pushed commit, for the caller to read.
    with contextlib.redirect_stdout(sys.stderr):
        lint_status = lint_site(site)
    if lint_status != 0:
        raise GitHubPagesError(
            "the site did not pass `check_site.py lint`; fix it before publishing"
        )
    _ensure_own_repo(site)
    owner = repo.split("/")[0]
    author = {
        "GIT_AUTHOR_NAME": owner,
        "GIT_AUTHOR_EMAIL": f"{owner}@users.noreply.github.com",
        "GIT_COMMITTER_NAME": owner,
        "GIT_COMMITTER_EMAIL": f"{owner}@users.noreply.github.com",
    }
    env = {**os.environ, **author}
    _git(site, "add", "-A")
    if _git(site, "status", "--porcelain"):
        _git(site, "commit", "-q", "-m", message, env=env)
    elif not _git(site, "rev-list", "--all"):
        raise GitHubPagesError(f"{site} has no files to publish")
    head = _git(site, "rev-parse", "HEAD")
    options, url = _gateway(repo)
    completed = subprocess.run(
        ["git", *options, "push", url, "HEAD:refs/heads/main"],
        cwd=site,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        output = completed.stderr.strip()
        hint = ""
        if "not permitted" in output.lower():
            hint = " -- the github-git-write permission is missing; request it again"
        elif "non-fast-forward" in output or "fetch first" in output:
            hint = " -- the repo has changes made outside this workspace; tell the user, never force-push"
        raise GitHubPagesError(f"the push failed: {output}{hint}")
    return head


def enable(repo: str) -> str:
    code, payload = api(
        "POST", f"/repos/{repo}/pages", {"source": {"branch": "main", "path": "/"}}
    )
    if code not in (201, 409):
        raise GitHubPagesError(
            f"Pages was not turned on ({code}): {json.dumps(payload)[:300]}"
        )
    code, payload = api("GET", f"/repos/{repo}/pages")
    if code != 200 or not isinstance(payload, dict):
        raise GitHubPagesError(
            f"cannot read the Pages settings ({code}): {json.dumps(payload)[:300]}"
        )
    return str(payload.get("html_url"))


def wait(repo: str, timeout_seconds: int, interval_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        code, payload = api("GET", f"/repos/{repo}/pages/builds/latest")
        if code == 200 and isinstance(payload, dict):
            status = payload.get("status")
            if status != last:
                print(f"build {status}", file=sys.stderr)
                last = status
            if status == "built":
                print(payload.get("commit", ""))
                return 0
            if status == "errored":
                error = payload.get("error") or {}
                print(
                    f"github_pages: the build failed: {error.get('message')}",
                    file=sys.stderr,
                )
                return 1
        time.sleep(interval_seconds)
    print(
        f"github_pages: no finished build after {timeout_seconds}s (last status: {last})",
        file=sys.stderr,
    )
    return 1


def disable(repo: str) -> None:
    code, payload = api("DELETE", f"/repos/{repo}/pages")
    if code not in (204, 404):
        raise GitHubPagesError(
            f"Pages was not turned off ({code}): {json.dumps(payload)[:300]}"
        )


def delete_repo(repo: str) -> None:
    code, payload = api("DELETE", f"/repos/{repo}")
    if code != 204:
        raise GitHubPagesError(
            f"the repo was not deleted ({code}): {json.dumps(payload)[:300]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a static site folder to GitHub Pages."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("whoami")
    create = commands.add_parser("create-repo")
    create.add_argument("--name", required=True)
    create.add_argument("--description", required=True)
    create.add_argument(
        "--owner", required=True, help="the GitHub username the user gave"
    )
    push_parser = commands.add_parser("push")
    push_parser.add_argument("--site", required=True, type=Path)
    push_parser.add_argument("--repo", required=True)
    push_parser.add_argument("--message", required=True)
    for name in ("enable", "disable"):
        commands.add_parser(name).add_argument("--repo", required=True)
    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("--repo", required=True)
    wait_parser.add_argument("--timeout-seconds", type=int, default=600)
    wait_parser.add_argument("--interval-seconds", type=int, default=10)
    delete = commands.add_parser("delete-repo")
    delete.add_argument("--repo", required=True)
    delete.add_argument("--yes-delete-forever", action="store_true", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "whoami":
            print(whoami())
        elif args.command == "create-repo":
            print(create_repo(args.name, args.description, args.owner))
        elif args.command == "push":
            print(push(args.site, args.repo, args.message))
        elif args.command == "enable":
            print(enable(args.repo))
        elif args.command == "wait":
            return wait(args.repo, args.timeout_seconds, args.interval_seconds)
        elif args.command == "disable":
            disable(args.repo)
        elif args.command == "delete-repo":
            delete_repo(args.repo)
    except GitHubPagesError as e:
        print(f"github_pages: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
