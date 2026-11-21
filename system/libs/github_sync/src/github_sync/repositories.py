"""Creating a GitHub repository, and closing it to outsiders, through latchkey.

The publish flows create the repository a template or an app is published to,
and `app-install link` creates an app's private dev repository; both go through
here. Every request body is built as JSON, so a description with quotes in it
cannot break the call, and the GitHub credential is injected server-side by
latchkey: no token is ever in the container.
"""

import json
import subprocess
from typing import Any

from github_sync.config import GithubSyncError

GITHUB_API_URL = "https://api.github.com"
# Every surface where someone who is not a collaborator could add content.
LOCKDOWN_SETTINGS = {
    "pull_request_creation_policy": "collaborators_only",
    "has_issues": False,
    "has_wiki": False,
    "has_projects": False,
    "has_discussions": False,
}

_LATCHKEY_CURL_TIMEOUT_SECONDS = 120
_NAME_TAKEN_MARKER = "already exists"
_NOT_PERMITTED_MARKER = "not permitted"


class GithubRequestError(GithubSyncError):
    """GitHub, or latchkey in front of it, refused a request."""


class RepositoryNameTakenError(GithubRequestError):
    """The account already has a repository of the name asked for."""


def request_github(method: str, url: str, body: dict[str, object]) -> dict[str, Any]:
    """Send one JSON request to the GitHub API through latchkey; its JSON answer.

    Raises GithubRequestError when latchkey cannot be run, exits non-zero, or
    answers with anything but a JSON object. An answer that reports a refusal
    is returned as it is, for the caller to read.
    """
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
            timeout=_LATCHKEY_CURL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GithubRequestError(f"cannot run latchkey: {e}") from e
    if completed.returncode != 0:
        raise GithubRequestError(
            f"{method} {url} failed (curl exit {completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise GithubRequestError(
            f"{method} {url} answered: {completed.stdout[:500]}"
        ) from e
    if not isinstance(response, dict):
        raise GithubRequestError(f"{method} {url} answered: {completed.stdout[:500]}")
    return response


def explain_refusal(response: dict[str, Any]) -> str:
    """A refusing answer as text, with what to do about the two refusals a
    caller can act on."""
    text = json.dumps(response)
    if _NOT_PERMITTED_MARKER in text:
        return (
            f"{text} -- the github-rest-api grant (github-read-user + github-write-all) "
            "is missing or too narrow; request it again"
        )
    if _NAME_TAKEN_MARKER in text:
        return f"{text} -- the name is taken on this account; ask the user for another"
    return text


def create_repository(name: str, description: str, is_private: bool) -> str:
    """Create the repository under the authenticated account and return its
    `<owner>/<repo>`.

    Raises RepositoryNameTakenError when the account already has one of that
    name, and GithubRequestError for every other refusal.
    """
    response = request_github(
        "POST",
        f"{GITHUB_API_URL}/user/repos",
        {"name": name, "description": description, "private": is_private},
    )
    owner = response.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(login, str) or response.get("name") != name:
        explanation = f"the repo was not created: {explain_refusal(response)}"
        if _NAME_TAKEN_MARKER in json.dumps(response):
            raise RepositoryNameTakenError(explanation)
        raise GithubRequestError(explanation)
    return f"{login}/{name}"


def lock_down_repository(repository: str) -> str | None:
    """Close the collaboration surface of `<owner>/<repo>`; the problem if it
    did not take, else None."""
    try:
        response = request_github(
            "PATCH", f"{GITHUB_API_URL}/repos/{repository}", dict(LOCKDOWN_SETTINGS)
        )
    except GithubRequestError as e:
        return str(e)
    if "name" not in response:
        return explain_refusal(response)
    return None
