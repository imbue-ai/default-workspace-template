"""Which ref to update to: the ``minds-v*`` release the workspace's release channel is
on, or an explicit override.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Collection, NamedTuple, Sequence

# A released Imbue Studio version tag, e.g. ``minds-v0.3.7`` (stable) or
# ``minds-v0.3.7-rc1`` (a release candidate -- a prerelease we never default to).
_TAG_RE = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")


class CeilingNotReleasedError(Exception):
    """Raised when the release this workspace is to update to cannot be had.

    A fault, unlike :class:`NoUpdateTargetError`: either the ceiling names no
    release at all, or it names one the template upstream does not carry. Both
    mean the release was never published as claimed, so the skill reports it and
    lets the user decide, rather than quietly taking some other release.
    """


class NoUpdateTargetError(ValueError):
    """Raised when no ref to update to could be chosen.

    A refusal, not a fault: the workspace is fine, there is simply nothing it may
    update to right now. Distinct from a plain ``ValueError`` so the CLI can render
    this case as a one-line explanation and let a genuine bug keep its traceback.
    """


class ResolvedTarget(NamedTuple):
    """The ref the update merges in, plus a coarse ``kind`` for the caller's log.

    ``kind`` is ``tag`` (a resolved ``minds-v*`` release), ``branch`` (a branch of
    the remote, qualified), or ``ref`` (any other override passed straight
    through for git to validate).

    ``ceiling`` is the newest release the workspace may take: the one its release
    channel is on, or the one the message that started the pass pinned, passed
    through as given. ``None`` means there is none to compare against, which an
    override in a workspace that has read no channel leaves. ``exceeds_ceiling``
    marks an override nothing vouches for: newer than the ceiling, or a
    branch/commit carrying no version to compare. The default path never sets it.
    """

    ref: str
    kind: str
    ceiling: str | None = None
    # Serialized as ``exceeds_ceiling``: every released SKILL.md reads that key,
    # and a rename would read as "false" to all of them.
    exceeds_ceiling: bool = False


class Version(NamedTuple):
    """A ``minds-v*`` tag's version, ordered by plain ``<`` the way semver orders.

    **Field order is the precedence order** -- comparison is tuple comparison, so
    reordering these silently changes which release outranks which.

    ``release_rank`` is 0 for a prerelease and 1 for the release it precedes, so
    ``0.4.0-rc1 < 0.4.0``; ``prerelease`` then breaks ties among prereleases of
    the same version. It has to be a *field* rather than a property derived from an
    empty ``prerelease``: only a field participates in the comparison.
    """

    major: int
    minor: int
    patch: int
    release_rank: int
    prerelease: tuple[tuple[int, int, str], ...]


def _prerelease_sort_key(pre: str) -> tuple[tuple[int, int, str], ...]:
    """Order a prerelease's dot-separated identifiers the way semver does.

    Numeric identifiers compare numerically and rank below alphanumeric ones, so
    ``rc.2`` follows ``rc.1`` rather than sorting lexically (where ``rc.10`` would
    land before ``rc.2``). Each identifier becomes ``(is_alphanumeric, number,
    text)`` so a single tuple comparison covers both kinds.

    "Numeric" is ``isdecimal``, semver's ``[0-9]+``, and not ``isdigit``, which
    also admits superscripts and other digits ``int()`` refuses to convert.
    """
    identifiers: list[tuple[int, int, str]] = []
    for identifier in pre.split("."):
        if identifier.isdecimal():
            identifiers.append((0, int(identifier), ""))
        else:
            identifiers.append((1, 0, identifier))
    return tuple(identifiers)


def parse_version(tag: str) -> Version | None:
    """Return the :class:`Version` of any ``minds-v*`` tag, prerelease included.

    Prereleases parse because a ceiling of ``minds-v0.4.0-rc1`` is a real version:
    it names that template as the target like any other release, and it vouches
    for an override the same way.

    Ordering follows semver: a prerelease sorts below its own release, so an
    override of ``minds-v0.4.0`` under a ``minds-v0.4.0-rc1`` ceiling exceeds it.

    Returns ``None`` only for something that is not a release tag at all (a
    branch name, a bare commit) -- there is genuinely no version to compare.
    """
    match = _TAG_RE.match(tag.strip())
    if match is None:
        return None
    pre = match.group("pre")
    return Version(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        release_rank=0 if pre is not None else 1,
        prerelease=_prerelease_sort_key(pre) if pre is not None else (),
    )


def _is_at_or_below_ceiling(ref: str, ceiling: str | None) -> bool:
    """Whether ``ref`` is provably a release at or below the ceiling.

    Both sides go through :func:`parse_version`, so a prerelease on either side
    compares properly rather than being written off. False for something with no
    version at all -- a branch or a bare commit -- which the ceiling cannot vouch
    for. True when there is no ceiling to compare against.
    """
    target_version = parse_version(ceiling) if ceiling is not None else None
    if target_version is None:
        return True
    ref_version = parse_version(ref)
    return ref_version is not None and ref_version <= target_version


def resolve_target(
    override: str | None,
    tags: Sequence[str],
    remote: str = "upstream",
    ceiling: str | None = None,
    remote_branches: Collection[str] = (),
) -> ResolvedTarget:
    """Resolve the update target ref.

    With no override the target is the ceiling itself: the ``minds-v*`` tag
    named by ``ceiling``, which must exist upstream. Anything else is a fault
    for the skill to judge, not a thing to work around here -- picking some
    other release would hand the workspace a template nobody chose for it.

    An override naming a branch of ``remote`` (``main`` always does; the rest are
    ``remote_branches``, the names under ``refs/remotes/<remote>/``) is
    **remote-qualified** to ``<remote>/<branch>``. A bare branch name resolves only
    through a *local* branch: a workspace usually has none for anything but its
    own, and one it does have is a copy ``git fetch upstream`` never advances --
    so the remote branch wins even over a local branch of the same name, or the
    pull would merge stale local code. A tag, by contrast, lands in the local tag
    namespace on fetch and resolves by its bare name, so a known-tag override is
    returned as-is. Any other override is passed through verbatim as a ``ref``
    for git to validate (so a user can pin an arbitrary commit or a ref they've
    already qualified themselves).

    An override is never silently blocked -- the user asked for it by name -- but
    one that is not provably at or below the ceiling comes back with
    ``exceeds_ceiling`` set, which the skill turns into an explicit user
    confirmation before anything is merged.
    """
    if override is None:
        if ceiling is None or parse_version(ceiling) is None:
            raise CeilingNotReleasedError(
                f"the version this workspace may update to is given as {ceiling!r}, which "
                f"is not a release tag, so there is no release to match. A development "
                f"build of Imbue Studio names its branch this way. Decide what this "
                f"workspace should take and pass it as --override."
            )
        if ceiling not in set(tags):
            raise CeilingNotReleasedError(
                f"this workspace is to update to {ceiling}, but the template upstream has no "
                f"such tag, so that release cannot be fetched. "
                f"Something is wrong with that release, not with this workspace: report it "
                f"rather than updating to a different version, unless the user names one."
            )
        return ResolvedTarget(ceiling, "tag", ceiling, False)
    exceeds = not _is_at_or_below_ceiling(override, ceiling)
    if override in set(tags):
        return ResolvedTarget(override, "tag", ceiling, exceeds)
    if override == "main" or override in set(remote_branches):
        return ResolvedTarget(f"{remote}/{override}", "branch", ceiling, exceeds)
    return ResolvedTarget(override, "ref", ceiling, exceeds)


def already_current_message(ref: str) -> str:
    return f"this workspace is already on {ref}; nothing to update"


# What the workspace keeps about its release channel, relative to its root: the update
# settings a client wrote (the channel, and the feed its file is read from), and the
# version the workspace's update dispatcher last read for that channel off the feed, at
# most once an hour. Both are written by ``system/libs/workspace_update_control``; this
# skill runs from a staged copy with no library beside it, so it reads the two files itself.
_UPDATE_SETTINGS_PATH = Path("data/.state/update-apply/settings.json")
_CHANNEL_VERSION_PATH = Path("data/.state/update-apply/channel-version.json")


class ChannelVersionUnavailableError(Exception):
    """Raised when the workspace has read no version for its release channel.

    Never downgraded to "take the newest release upstream": the channel's version
    is the newest the workspace may take, and which release to take without one
    is the user's to say.
    """


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def read_channel_template_ref(repo_root: Path) -> str:
    """Return the release the workspace's release channel is on, as the workspace last read it.

    The workspace reads its channel's file itself, so this asks nothing outside
    it: a workspace whose owner has no Imbue Studio desktop app running is
    answered like any other. Each way of having no version says what to do next,
    because the user's next action differs.
    """
    settings = _read_json_object(repo_root / _UPDATE_SETTINGS_PATH)
    if settings is None:
        raise ChannelVersionUnavailableError(
            "this workspace holds no update settings, so it follows no release channel "
            "and there is no release to update to by default. Imbue Studio writes them "
            "when it opens the workspace: open the workspace there and run the update "
            "again, or name the version to update to."
        )
    channel = settings.get("channel")
    read = _read_json_object(repo_root / _CHANNEL_VERSION_PATH) or {}
    is_this_channels = read.get("channel") == channel and read.get(
        "feed_base_url"
    ) == settings.get("feed_base_url")
    template_ref = read.get("template_ref") if is_this_channels else None
    if isinstance(template_ref, str) and template_ref:
        return template_ref
    why = read.get("error") if is_this_channels else None
    raise ChannelVersionUnavailableError(
        f"this workspace has read no version for its release channel ({channel})"
        + (f": {why}" if isinstance(why, str) and why else "")
        + ". It asks the release feed at most once an hour, so run the update again "
        "later, or name the version to update to."
    )


def is_workspace_without_update_settings(repo_root: Path) -> bool:
    """Whether no client has written update settings into this workspace, so it follows no release channel."""
    return _read_json_object(repo_root / _UPDATE_SETTINGS_PATH) is None


# CLEANUP: everything below, with ``is_workspace_without_update_settings`` above and their use in
# ``update_self.py``, goes once no supported Imbue Studio desktop app predates the update settings
# (the first with them is the one that ships phase 5 of the web and desktop consolidation). Such
# an app writes no settings into a workspace, so a workspace it alone drives follows no channel,
# and an update started by hand there asks that app which release it supports.
# The Imbue Studio app's version route, addressed through the latchkey gateway's
# ``minds-api-proxy`` on the reserved gateway-self host. Allowed by the agent
# permissions baseline (``minds-app-version-read``), so this needs no grant and
# never raises a permission dialog -- which matters because update-self resolves
# its target from a background worker, with nobody watching to approve one.
_MINDS_APP_VERSION_URL = (
    "http://latchkey-self.invalid/minds-api-proxy/api/v1/app/version"
)

# Bounds the gateway round-trip, at the house network default (the style guide's
# 60s, matching this repo's other ``latchkey curl``, ``github_sync``'s
# ``_LATCHKEY_CURL_TIMEOUT_SECONDS``).
_APP_VERSION_TIMEOUT_SECONDS = 60

# Statuses that mean "this app predates the version route", not "something went
# wrong". 404 is the obvious one; 403 is in fact the *likelier* of the two, since
# the route and the gateway permission that reaches it (``minds-app-version-read``)
# ship in the same release, so an app old enough to lack the route also lacks the
# grant -- and the gateway denies an ungranted request before the app ever sees it.
_APP_TOO_OLD_STATUSES = frozenset({"403", "404"})


class DesktopVersionUnavailableError(Exception):
    """Raised when the Imbue Studio app's own version could not be read.

    Never downgraded to "no version to match": an app that cannot answer is very
    often an app too old to *have* this route, which is exactly the case that
    matching the app protects against.
    """


def fetch_desktop_template_ref(url: str = _MINDS_APP_VERSION_URL) -> str:
    """Return the newest workspace-template ref the running Imbue Studio app supports.

    Goes through ``latchkey curl``, which injects the gateway credentials and
    passes every other argument (and curl's exit code) straight through. Each
    failure mode is reported distinctly, because the user's next action differs:
    a transport failure is worth retrying, an :data:`_APP_TOO_OLD_STATUSES`
    answer (403 or 404) means the app must be updated first, and any other bad
    status or malformed body is a bug worth reporting.
    """
    with tempfile.NamedTemporaryFile(suffix=".json") as body_file:
        try:
            result = subprocess.run(
                [
                    "latchkey",
                    "curl",
                    "--silent",
                    "--show-error",
                    "--output",
                    body_file.name,
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=_APP_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DesktopVersionUnavailableError(
                f"could not reach the Imbue Studio app to read its version ({e}). The app may be "
                f"closed or the gateway down; retry once it is running."
            ) from e
        if result.returncode != 0:
            raise DesktopVersionUnavailableError(
                f"could not reach the Imbue Studio app to read its version (latchkey curl exited "
                f"{result.returncode}: {result.stderr.strip()}). The app may be closed or the "
                f"gateway down; retry once it is running."
            )
        status = result.stdout.strip()
        body = Path(body_file.name).read_text()

    if status in _APP_TOO_OLD_STATUSES:
        raise DesktopVersionUnavailableError(
            "this workspace's Imbue Studio app is too old to report its version (it answered "
            f"HTTP {status} for {url}), so there is no way to tell how far this workspace "
            "may safely update. Update the Imbue Studio app itself first."
        )
    if status != "200":
        raise DesktopVersionUnavailableError(
            f"the Imbue Studio app returned HTTP {status} for its version ({body.strip()[:200]})."
        )
    try:
        template_ref = json.loads(body)["workspace_template_ref"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise DesktopVersionUnavailableError(
            f"the Imbue Studio app's version response could not be parsed ({e}): {body.strip()[:200]}"
        ) from e
    if not isinstance(template_ref, str) or not template_ref:
        raise DesktopVersionUnavailableError(
            f"the Imbue Studio app reported an empty workspace_template_ref: {body.strip()[:200]}"
        )
    return template_ref
