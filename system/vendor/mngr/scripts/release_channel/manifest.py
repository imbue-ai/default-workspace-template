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

So the artifacts are never re-hosted and the digests are never recomputed: a
channel serves the same signed, notarized bytes ToDesktop does, from ToDesktop's
own CDN. The manifest itself is unsigned -- it is what tells the client which
digest to expect, so whoever can write the bucket decides that, and access to the
bucket is the only thing protecting it. One manifest lists every arch and the
client picks, which is why promoting a channel uploads one file rather than four.
Every channel goes through this, ``stable`` included.

The gates below guard the write because the failures they catch are otherwise
silent: a build whose pre-baked Lima image is missing turns every create into a
slow in-VM build without anything turning red, and an artifact url that loses its
extension stops being the one electron-updater selects. A channel moving
backwards is reported rather than refused -- it changes what a new download gets
and moves nobody who already has the newer build.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from typing import Annotated
from typing import Any
from typing import Final

import yaml
from botocore.exceptions import ClientError
from pydantic import Field
from pydantic import StrictInt
from pydantic import TypeAdapter
from pydantic import ValidationError

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

# electron-updater yaml's key name for the rollout, not the staging tier.
_ROLLOUT_KEY: Final[str] = "stagingPercentage"

_REFERENCE_KEYS: Final[frozenset[str]] = frozenset({"url", "path"})

# What electron-updater serves when the key is absent or null.
FULL_ROLLOUT_PERCENTAGE: Final[int] = 100

RolloutPercentage = Annotated[StrictInt, Field(ge=0, le=FULL_ROLLOUT_PERCENTAGE)]
_ROLLOUT_PERCENTAGE = TypeAdapter(RolloutPercentage)


class PromotionError(Exception):
    """A promotion refused before writing anything."""


# We start from ToDesktop's manifest (binary url, sha, etc.),
# make modifications (resolve the relative urls, add the rollout percentage, etc.) and then publish to r2 bucket.
Manifest = Mapping[str, Any]


def version_of(manifest: Manifest) -> str:
    """Guaranteed present: ``parse_manifest`` is the only way one is read in."""
    return str(manifest["version"])


def render(manifest: Manifest) -> str:
    """The document as the client will read it."""
    return yaml.dump(dict(manifest), sort_keys=False)


def channel_filename(channel: str) -> str:
    """The object electron-updater asks a generic feed for, e.g. ``alpha-mac.yml``.

    One definition for the read and the write, because a drift between them is
    silent both ways: the promotion writes an object no client ever fetches, and
    the read of what a channel serves asks for a name nothing publishes, finds
    nothing, and reports every move as a first publish.
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


def parse_manifest(manifest_text: str, source: str) -> Manifest:
    """``source`` names the document, because two different ones reach here.

    ToDesktop's build manifest and the channel's own manifest fail identically
    otherwise, and they mean different things: pick another build, versus the
    feed bucket holds something the publish path should never have written.
    """
    try:
        document = yaml.safe_load(manifest_text)
    except yaml.YAMLError as exc:
        raise PromotionError(f"{source} is not valid YAML: {exc}.") from exc
    if not isinstance(document, dict):
        raise PromotionError(f"{source} is not a YAML mapping.")
    if "version" not in document:
        raise PromotionError(f"{source} has no `version:` key.")
    return document


def rewrite_manifest(manifest_text: str, app_id: str) -> Manifest:
    """Make every artifact reference absolute, leaving digests and sizes untouched.

    ``newUrlFromBase`` is ``new URL(pathname, baseUrl)``, so an absolute URL in
    the manifest wins over the feed's own base -- which is what lets a manifest
    we host point at artifacts ToDesktop hosts.
    """
    manifest = parse_manifest(manifest_text, "ToDesktop's build manifest")
    base = f"{TODESKTOP_FEED}/{app_id}/"
    document = _with_absolute_references(manifest, base)
    if not list(_references(document)):
        raise PromotionError("Manifest lists no artifacts.")
    return document


def _with_absolute_references(node: Any, base: str) -> Any:
    """Every ``url`` and ``path`` made absolute, at whatever depth it sits."""
    if isinstance(node, dict):
        return {
            key: _absolute_reference(value, base)
            if key in _REFERENCE_KEYS and isinstance(value, str)
            else _with_absolute_references(value, base)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_with_absolute_references(item, base) for item in node]
    return node


def _references(node: Any) -> Iterator[str]:
    """Every ``url`` and ``path`` the document names, at whatever depth it sits."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _REFERENCE_KEYS and isinstance(value, str):
                yield value
            else:
                yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


def _absolute_reference(value: str, base: str) -> str:
    absolute = value if value.startswith(("http://", "https://")) else base + urllib.parse.quote(value)
    if not absolute.lower().endswith(_REQUIRED_EXTENSIONS):
        raise PromotionError(
            f"Refusing to publish {absolute!r}: electron-updater selects the macOS artifact by "
            f"URL extension, so every url must end in one of {_REQUIRED_EXTENSIONS}."
        )
    return absolute


def read_rollout_percentage(manifest: Manifest, source: str) -> int | None:
    """The rollout percentage a manifest declares, or None when it declares none.

    Every manifest published before rollouts existed reads as None. ``source``
    names the object, because one run reads three of them and the refusal below
    asks for a hand edit to whichever one it was.
    """
    declared = manifest.get(_ROLLOUT_KEY)
    if declared is None:
        return None
    try:
        return _ROLLOUT_PERCENTAGE.validate_python(declared)
    except ValidationError as exc:
        raise PromotionError(
            f"{source} declares {_ROLLOUT_KEY} {declared!r}, which is not a rollout percentage: "
            f"{exc.errors()[0]['msg'].lower()}. electron-updater refuses none of these -- a non-numeric value "
            f"reaches everyone and a float truncates. Fix the object in the bucket by hand."
        ) from exc


def with_rollout_percentage(manifest: Manifest, percentage: int) -> Manifest:
    """Declare the rollout percentage, replacing whatever the build manifest carried."""
    return {**manifest, _ROLLOUT_KEY: percentage}


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
            f"Publish it (see apps/minds/docs/deploy/ops/app-release.md) or clients will silently build in-VM."
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
    return parse_manifest(text, url)


def assert_plain_release_version(version: str) -> None:
    """A version is stamped once at cut, so promotion is a pointer move.

    A prerelease suffix means the promoted build would have to be rebuilt under a
    new version -- and then the bytes that soaked are not the bytes that ship.
    """
    _version_key(version)


def is_a_version_decrease(current: str | None, incoming: str) -> bool:
    """Whether this moves a channel to an older version, which is a supported move.

    Nobody is pulled back: ``allowDowngrade`` is false, so an install that took
    the newer version stays there until a release passes it. What it changes is
    what a *new* download gets.
    """
    if current is None:
        return False
    return _version_key(incoming) < _version_key(current)


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
    window reads the *previous* one back through the feed -- which then names the
    wrong served state, settles the BACKWARDS label against a version the channel
    has already left, and republishes a promotion already applied. The bucket
    holds the object itself and R2 is read-after-write consistent on it, so this
    cannot be stale.

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
    return parse_manifest(text, f"{bucket}/{key}")


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
        Body=render(manifest).encode("utf-8"),
        ContentType="text/yaml",
        CacheControl=f"public, max-age={cache_seconds}",
    )
    return key
