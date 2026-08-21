"""One channel's update manifest: the gates on writing it, the rewrite, the upload.

The single-channel primitive, pointing a channel at an already-built ToDesktop
build. ``publish.py`` composes these from ``apps/minds/release-channels.toml``,
which is the only thing that publishes a manifest, so there is no entry point
here.

Promotion is a metadata operation, never a rebuild. ToDesktop already published
a complete update manifest for every build it made -- released or not -- at
``<feed>/latest-mac-build-<buildId>.yml``, carrying the version, the per-arch
filenames, their sizes, and their sha512 digests. Promoting a channel copies
that manifest, rewrites its relative ``url:`` fields to absolute ones pointing
back at ToDesktop's CDN, and uploads the result as ``<channel>-mac.yml``.

So the artifacts are never re-hosted and the digests are never recomputed: the
bytes a channel serves are the exact bytes ToDesktop signed and notarized. One
manifest lists every arch and the client picks, which is why promoting a channel
uploads one file rather than four. Every channel goes through this, ``stable``
included.

The gates below guard the write because both failures are otherwise silent -- a
channel moving backwards, and a build whose pre-baked Lima image is missing,
which turns every create into a slow in-VM build without anything turning red.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import Final

import click
from botocore.exceptions import ClientError

from scripts.r2.client import read_r2_credentials
from scripts.r2.client import s3_client

TODESKTOP_FEED: Final[str] = "https://download.todesktop.com"

PUBLISHABLE_CHANNELS: Final[tuple[str, ...]] = ("stable", "beta", "alpha")

# electron-updater's MacUpdater picks the artifact by URL *pathname extension*
# (findFile(files, "zip", ["pkg", "dmg"])), so a rewritten URL that loses its
# .zip suffix stops being selected as the zip and only survives via a
# not-pkg-and-not-dmg fallback. Verified against electron-updater 6.8.9.
_REQUIRED_EXTENSIONS: Final[tuple[str, ...]] = (".zip", ".dmg")

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")


class PromotionError(Exception):
    """A promotion refused before writing anything."""


@dataclass(frozen=True)
class Manifest:
    """A parsed electron-updater channel manifest."""

    version: str
    text: str


def channel_filename(channel: str) -> str:
    """The object electron-updater asks a generic feed for, e.g. ``alpha-mac.yml``.

    One definition for the read and the write, because a drift between them is
    silent both ways: the promotion writes an object no client ever fetches, and
    the rollback gate reads a name nothing publishes, finds nothing, and waves
    every move through as a first publish.
    """
    return f"{channel}-mac.yml"


def http_get(url: str) -> bytes:
    """The real network, and the default every ``Fetch`` parameter here falls back to."""
    request = urllib.request.Request(url, headers={"User-Agent": "minds-release-channels"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


Fetch = Callable[[str], bytes]


def fetch_build_manifest(app_id: str, build_id: str, fetch: Fetch = http_get) -> str:
    """Fetch ToDesktop's own per-build manifest, which exists for unreleased builds too."""
    url = f"{TODESKTOP_FEED}/{app_id}/latest-mac-build-{build_id}.yml"
    try:
        return fetch(url).decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise PromotionError(f"No ToDesktop manifest for build {build_id} ({url} returned {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise PromotionError(f"Cannot reach {url}: {exc.reason}.") from exc


def parse_version(manifest_text: str, source: str) -> str:
    """``source`` names the document, because two different ones reach here.

    ToDesktop's build manifest and the channel's own manifest fail identically
    otherwise, and they mean different things: pick another build, versus the
    feed bucket holds something the publish path should never have written.
    """
    for line in manifest_text.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise PromotionError(f"{source} has no `version:` line.")


def rewrite_manifest(manifest_text: str, app_id: str) -> Manifest:
    """Make every artifact reference absolute, leaving digests and sizes untouched.

    ``newUrlFromBase`` is ``new URL(pathname, baseUrl)``, so an absolute URL in
    the manifest wins over the feed's own base -- which is what lets a manifest
    we host point at artifacts ToDesktop hosts.
    """
    base = f"{TODESKTOP_FEED}/{app_id}/"
    rewritten: list[str] = []
    seen_artifact = False
    for line in manifest_text.splitlines():
        match = re.match(r"^(\s*(?:-\s+)?(?:url|path):\s*)(\S.*)$", line)
        if match is None:
            rewritten.append(line)
            continue
        prefix, value = match.groups()
        value = value.strip()
        if value.startswith("http://") or value.startswith("https://"):
            absolute = value
        else:
            absolute = base + urllib.parse.quote(value)
        if not absolute.lower().endswith(_REQUIRED_EXTENSIONS):
            raise PromotionError(
                f"Refusing to publish {absolute!r}: electron-updater selects the macOS artifact by "
                f"URL extension, so every url must end in one of {_REQUIRED_EXTENSIONS}."
            )
        seen_artifact = True
        rewritten.append(prefix + absolute)
    if not seen_artifact:
        raise PromotionError("Manifest lists no artifacts.")
    text = "\n".join(rewritten) + "\n"
    return Manifest(version=parse_version(text, "ToDesktop's build manifest"), text=text)


def assert_lima_image_published(
    lima_image_base_url: str, fallback_branch: str, arches: tuple[str, ...], fetch: Fetch = http_get
) -> None:
    """Fail loudly when the tag the binary asks for has no image.

    Without this the failure is invisible: clients get VERSION_UNAVAILABLE and
    silently build in-VM instead.
    """
    url = f"{lima_image_base_url.rstrip('/')}/manifests/{fallback_branch}/root.json"
    try:
        body = fetch(url)
    except urllib.error.HTTPError as exc:
        raise PromotionError(
            f"No Lima image published for {fallback_branch} ({url} returned {exc.code}). "
            f"Publish it (see apps/minds/docs/deploy/release.md) or clients will silently build in-VM."
        ) from exc
    except urllib.error.URLError as exc:
        # An unreachable image host cannot be told apart from an unpublished
        # image, and both end the same way for the user, so refuse either way
        # rather than let a DNS failure read as a passing gate.
        raise PromotionError(f"Cannot reach the Lima image store at {url}: {exc.reason}.") from exc
    try:
        manifest = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"The Lima image manifest at {url} is not valid JSON: {exc}.") from exc
    named = manifest.get("minds_version")
    if named != fallback_branch:
        raise PromotionError(f"Lima image manifest at {url} names {named!r}, not {fallback_branch!r}.")
    # The root manifest's canonical arch keys are upper case (AARCH64 / X86_64),
    # while `--arch` takes the lower-case spelling the bake and the runbook use.
    present = {str(entry.get("arch", "")).upper() for entry in manifest.get("entries", [])}
    missing = sorted(arch for arch in arches if arch.upper() not in present)
    if missing:
        raise PromotionError(
            f"Lima image manifest {url} is missing arch(es) {missing}; those users take the slow path."
        )


def read_channel_manifest_from_feed(feed_base_url: str, channel: str, fetch: Fetch = http_get) -> Manifest | None:
    """What a channel serves now as the public feed reports it, or None when it has never been published.

    The whole manifest rather than its version, because a channel is declared by
    build id and two builds can carry the same version -- the version is stamped
    once at cut, and every build between cuts repeats it.
    """
    url = f"{feed_base_url.rstrip('/')}/{channel_filename(channel)}"
    try:
        text = fetch(url).decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        # Only a 404 means "never published". Anything else leaves the current
        # version unknown, which is the one state a promotion must not proceed
        # from -- and it is reported the same way as every other failure here,
        # because a traceback in the job output helps nobody reviewing a promotion.
        raise PromotionError(f"Cannot read the current {channel} version: {url} returned {exc.code}.") from exc
    except urllib.error.URLError as exc:
        # Never mistake an unreachable feed for "nothing published yet": that
        # would turn a network blip into an unguarded overwrite.
        raise PromotionError(f"Cannot reach {url} to read the current {channel} version: {exc.reason}.") from exc
    return Manifest(version=parse_version(text, url), text=text)


def assert_not_a_rollback(channel: str, current: str | None, incoming: str, allow_rollback: bool) -> None:
    # Keyed before the never-published early return, because _version_key is
    # also where the plain-X.Y.Z rule is enforced, and it applies to a channel's
    # first publish exactly as much as to its tenth.
    incoming_key = _version_key(incoming)
    if current is None:
        return
    if incoming_key >= _version_key(current):
        return
    if not allow_rollback:
        raise PromotionError(
            f"{channel} currently serves {current} and this would move it back to {incoming}. "
            f"Pass --allow-rollback if you mean to withdraw a build. Note that users who already "
            f"took {current} stay on it -- allowDowngrade is false -- so this only stops new installs."
        )
    click.echo(f"Rolling {channel} back from {current} to {incoming}.", err=True)


def _version_key(version: str) -> tuple[int, ...]:
    if not _VERSION_RE.match(version):
        raise PromotionError(f"Version {version!r} is not a plain X.Y.Z release version.")
    return tuple(int(part) for part in version.split("."))


def r2_client() -> Any:
    """The real feed bucket, and the default every ``make_client`` parameter here falls back to."""
    return s3_client(read_r2_credentials(os.environ))


MakeS3Client = Callable[[], Any]


def read_channel_manifest_from_bucket(
    bucket: str, channel: str, make_client: MakeS3Client = r2_client
) -> Manifest | None:
    """What a channel serves now, read from the bucket rather than through the CDN.

    The manifest is uploaded with a short max-age, so a promotion run inside that
    window reads the *previous* one back through the feed -- and that is the one
    answer the rollback gate must never get, because it would wave through the
    backwards move the gate exists to refuse. The bucket holds the object itself
    and R2 is read-after-write consistent on it, so this cannot be stale.

    Needs a credential, which is why the public reader still exists: the
    validate job deliberately holds none.
    """
    client = make_client()
    key = channel_filename(channel)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except ClientError as exc:
        # Only an absent key means "never published". Anything else -- a denied
        # read, a throttle -- leaves the current version unknown, which is the
        # one state a promotion must not proceed from.
        raise PromotionError(f"Cannot read the current {channel} version from {bucket}/{key}: {exc}.") from exc
    text = response["Body"].read().decode("utf-8")
    return Manifest(version=parse_version(text, f"{bucket}/{key}"), text=text)


def read_current_channel_manifest(
    channel: str,
    *,
    bucket: str,
    feed_base_url: str,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
) -> Manifest | None:
    """What a channel serves now, from whichever source the run can reach.

    The bucket is the better answer and needs a credential; the feed is the
    fallback for the validate job, which holds none. See ``publish.py``, which
    decides and says which one it used.
    """
    if from_bucket:
        return read_channel_manifest_from_bucket(bucket, channel, make_client=make_client)
    return read_channel_manifest_from_feed(feed_base_url, channel, fetch=fetch)


def upload_manifest(
    manifest: Manifest, *, bucket: str, channel: str, cache_seconds: int, make_client: MakeS3Client = r2_client
) -> str:
    """Write the channel manifest to R2 with a short TTL.

    The manifest is the only mutable object in the system and every client polls
    it, so a long CDN TTL silently becomes the promotion latency.
    """
    key = channel_filename(channel)
    make_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=manifest.text.encode("utf-8"),
        ContentType="text/yaml",
        CacheControl=f"public, max-age={cache_seconds}",
    )
    return key
