"""Which ref to update to: the ``minds-v*`` release the workspace's release channel is
on, or an explicit override.
"""

from __future__ import annotations

import json
import re
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
