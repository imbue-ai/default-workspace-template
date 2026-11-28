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
# What a refused push most likely means, matched against git's stderr. Ordered so
# the specific causes win: GitHub's workflow-scope refusal is also printed as a
# "[remote rejected]" line. The stderr also carries the remote URL, so no needle
# may be something a repo name can contain.
_PUSH_FAILURE_HINTS = (
    (
        ("create or update workflow", "`workflow` scope"),
        "the stored GitHub credential cannot push .github/workflows/ files; tell the "
        "user and stop -- never strip files to get the push through",
    ),
    (
        (_NOT_PERMITTED_MARKER,),
        "the github-git-write permission is missing; request it again",
    ),
    (
        ("http 413", "too large"),
        "the user's Imbue Studio app is older than the gateway's raised body cap and "
        "cannot proxy a push this size; tell the user and stop",
    ),
    (
        ("(non-fast-forward)", "fetch first"),
        "the published main moved since its tip was read; someone else pushed there. "
        "Stop and tell the user -- never force-push over it",
    ),
)


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
    name, and GithubRequestError for every other refusal, and when GitHub's
    answer does not say the repository has the visibility asked for: a
    repository that was to be private is never taken to be so unconfirmed.
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
    if response.get("private") is not is_private:
        wanted = "private" if is_private else "public"
        raise GithubRequestError(
            f"{login}/{name} was created, but GitHub did not confirm it is {wanted} "
            f"(it answered private={json.dumps(response.get('private'))}): nothing was "
            "pushed to it; check it on GitHub, and delete it there before trying again"
        )
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


def make_repository_public(repository: str) -> None:
    """Make `<owner>/<repo>` public, confirmed by the answer to the change
    itself: the access a publish holds changes a repository's settings and
    does not read them.

    Raises GithubRequestError when the answer does not say it is public.
    """
    response = request_github(
        "PATCH", f"{GITHUB_API_URL}/repos/{repository}", {"private": False}
    )
    if response.get("private") is not False:
        raise GithubRequestError(
            f"{repository} was not made public: {explain_refusal(response)}"
        )


def set_repository_topics(repository: str, names: list[str]) -> None:
    """Replace the topics of `<owner>/<repo>` with `names`, which is how
    published repositories of one kind are found as a group.

    Raises GithubRequestError when the topics were not set.
    """
    response = request_github(
        "PUT", f"{GITHUB_API_URL}/repos/{repository}/topics", {"names": names}
    )
    answered = response.get("names")
    if not isinstance(answered, list) or any(name not in answered for name in names):
        raise GithubRequestError(f"the topic was not set: {explain_refusal(response)}")


def hint_for_push_failure(output: str) -> str | None:
    """What to do about a push git refused with `output`, when the refusal is
    one of the known ones; None otherwise."""
    lowered = output.lower()
    for needles, hint in _PUSH_FAILURE_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return None
