"""Fetching the release tags a checkout lacks, so that it can name its own version.

A workspace made from a published copy of the template holds the template's history and none of its tags. Its
checkout then names no version (``installed_version``): its row shows none, and the workspace never finds itself
behind its release channel, so it never arms an update. The tags are fetched from the template the workspace
updates from, which is the one the update-self skill fetches from too: the ``upstream`` remote where the checkout
has one, and otherwise the address ``parent.toml`` names, which is what the skill makes that remote from. The
version a workspace reads and the updates it takes therefore come from one place.

Two checkouts are left alone, since the tags could not name their version. One with no ``parent.toml`` was not
made from the template: the fetch would write every release's objects into a repository of the user's own. A
shallow one lacks the history between its head and any tag.

An attempt is remembered, and the next is made a day later at the earliest, so a checkout the tags cannot name
costs one fetch a day.
"""

import re
import tomllib
from typing import Final

from workspace_update_control.dispatcher import (
    UPDATE_PROGRAM_SCHEMA,
    read_json_object,
    write_json_atomically,
    write_log_line,
)
from workspace_update_control.protocol import RELEASE_TAG_FETCH_FILE_PATH
from workspace_update_control.support import run_command

# Where a checkout made from the template records the template it updates from: under ``system/config`` from
# minds-v0.3.10 on, and at the root before that.
_PARENT_FILE_PATHS: Final[tuple[str, ...]] = ("system/config/parent.toml", "parent.toml")
# The remote the update-self skill fetches from.
_UPSTREAM_REMOTE_NAME: Final[str] = "upstream"
# Only the release tags: nothing else the template holds can name a version.
_RELEASE_TAG_REFSPEC: Final[str] = "refs/tags/minds-v*:refs/tags/minds-v*"
RELEASE_TAG_FETCH_RETRY_SECONDS: Final[float] = 24 * 60 * 60
_GIT_TIMEOUT_SECONDS: Final[float] = 10.0
# The pattern takes every release, the ones newer than the checkout's base among them, whose commits a published
# copy does not hold: tens of megabytes. A fetch killed part-way keeps nothing, so the bound has to cover the
# whole transfer. It runs inside the dispatcher's minute, which cron does not start again while one is running.
_FETCH_TIMEOUT_SECONDS: Final[float] = 120.0
_ERROR_MAX_CHARS: Final[int] = 300
# The user and password an address may carry (``https://<token>@host/...``), as a private template's may: all
# up to the last ``@`` before the path, since a password may hold one itself.
_URL_CREDENTIALS_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<=://)[^/\s]*@")


def _parent_url() -> str | None:
    """The address the checkout's ``parent.toml`` names ('' when it names none); None for a checkout with no
    such file, which was not made from the template."""
    for path in _PARENT_FILE_PATHS:
        try:
            with open(path, "rb") as handle:
                url = tomllib.load(handle).get("url")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            write_log_line("could not read %s (%s)" % (path, exc))
            return ""
        return url.strip() if isinstance(url, str) else ""
    return None


def _git_line(*arguments: str) -> str:
    """The one line a read-only git command prints; '' when it failed or could not run."""
    result = run_command(("git", *arguments), _GIT_TIMEOUT_SECONDS)
    return result.stdout.strip() if result.returncode == 0 else ""


def _without_url_credentials(text: str) -> str:
    """``text`` with the user and password dropped from every address in it."""
    return _URL_CREDENTIALS_PATTERN.sub("", text)


def _is_fetch_due(now: float) -> bool:
    kept = read_json_object(RELEASE_TAG_FETCH_FILE_PATH)
    attempted_at = kept.get("attempted_at") if kept is not None else None
    if isinstance(attempted_at, bool) or not isinstance(attempted_at, (int, float)):
        return True
    # A clock that went back must not put the next attempt off for good.
    return not 0 <= now - attempted_at < RELEASE_TAG_FETCH_RETRY_SECONDS


def fetch_release_tags(now: float) -> bool:
    """Fetch the template's release tags into the checkout at the working directory; whether a fetch was made,
    after which the checkout may name a version it did not before.

    Nothing is fetched into a checkout the tags could not name (one with no ``parent.toml``, or a shallow one),
    or within ``RELEASE_TAG_FETCH_RETRY_SECONDS`` of the last attempt. The caller makes sure no update is
    running: a run fetches into the same checkout, and two fetches would write one checkout's refs.
    """
    parent_url = _parent_url()
    if parent_url is None or not _is_fetch_due(now):
        return False
    if _git_line("rev-parse", "--is-shallow-repository") != "false":
        return False
    url = _git_line("remote", "get-url", _UPSTREAM_REMOTE_NAME) or parent_url
    if not url:
        return False
    write_json_atomically(
        RELEASE_TAG_FETCH_FILE_PATH,
        {"schema": UPDATE_PROGRAM_SCHEMA, "attempted_at": now},
    )
    # ``--no-tags`` keeps git from following the template's other tags into the checkout beside the releases.
    try:
        fetch = run_command(
            ("git", "fetch", "--quiet", "--no-tags", url, _RELEASE_TAG_REFSPEC),
            _FETCH_TIMEOUT_SECONDS,
        )
    except UnicodeDecodeError as exc:
        # Git relays what the template's server sent it, which need not be text, and says so whether or not the
        # tags came.
        write_log_line(
            "could not read what the fetch of the template's release tags printed (%s)"
            % exc
        )
        return True
    if fetch.returncode == 0:
        write_log_line("fetched the template's release tags")
    else:
        # The error may name the address fetched from: git's own, or the whole command when the fetch overran.
        write_log_line(
            "could not fetch the template's release tags: %s"
            % _without_url_credentials(fetch.stderr.strip())[-_ERROR_MAX_CHARS:]
        )
    return True
