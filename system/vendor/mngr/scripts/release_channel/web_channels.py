"""The web channels: which template tag browser-created workspaces pin to, per channel.

The hosted web chrome creates workspaces through the connector's ``POST
/hosts/claim``, which leases a pool host baked at exactly one template tag and
has no rebuild fallback. The tag is a channel pointer rather than a value
frozen into the connector at deploy time, so a connector deploy never moves
it: each ``[web_channels.<channel>]`` entry in
``apps/minds/release-channels.toml`` names the tag, this publishes it as
``<channel>-web.json`` beside the desktop manifests, and the connector reads
that file (cached briefly) on every web create -- so moving the tag is a
promotion PR.

Independent of the desktop ``[channels.*]`` entries on purpose: the web client
has no ToDesktop build, so nothing ties its tag to a desktop version, and a
web-only template fix moves ahead of (or behind) the desktop on its own.

Publishing gates on the tag existing on the default-workspace-template remote
(the same check the pool bake's ``--from-tag`` runs), and nothing else: the
workflow holds no pool credentials, so whether the pool is actually baked at
the tag stays the operator's job (see apps/minds/docs/deploy/ops/pool-hosts.md).

This module is deliberately self-contained: it names, reads and writes its own
object with the stable primitives of ``manifest.py`` rather than its channel
read/upload helpers. CLEANUP: fold the web platform into the per-platform
publishing machinery that mngr/linux-packaging (imbue-ai/mngr-internal#943)
adds to ``manifest.py`` / ``publish.py`` once both that branch and this one
are on main -- owed by whichever of the two merges second.
"""

import functools
import json
import re
import subprocess
import tomllib
import urllib.error
from collections.abc import Callable
from typing import Final

from botocore.exceptions import ClientError
from pydantic import StrictStr
from pydantic import ValidationError
from pydantic import field_validator

from imbue.imbue_common.frozen_model import FrozenModel
from scripts.release_channel.manifest import Fetch
from scripts.release_channel.manifest import MakeS3Client
from scripts.release_channel.manifest import Manifest
from scripts.release_channel.manifest import PUBLISHABLE_CHANNELS
from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.manifest import http_get
from scripts.release_channel.manifest import r2_client

WEB_CHANNELS_TABLE: Final[str] = "web_channels"

# The web client's platform name, spelled the way the channel files are:
# ``<channel>-web.json`` beside ``<channel>-mac.yml``. JSON rather than YAML
# because this file's shape is ours (the desktop manifests are YAML by
# electron-updater's contract), and the connector reads it with the stdlib.
WEB_PLATFORM: Final[str] = "web"

# The manifest key the connector reads the tag out of. The connector cannot
# import this module (scripts do not ship into its container), so its
# ``web_template_channel.py`` repeats the literal; both name the other.
TEMPLATE_REF_KEY: Final[str] = "templateRef"

DEFAULT_WORKSPACE_TEMPLATE_REMOTE: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"

# A web pin is a release tag, never a branch: web claims lease an exact match
# and derive the slice-fleet generation cap from the tag's version.
_TEMPLATE_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^minds-v(\d+\.\d+\.\d+)$")

_LS_REMOTE_TIMEOUT_SECONDS: Final[int] = 60


class WebChannelEntry(FrozenModel):
    """One web channel's declared state: the template tag its browser creates pin to."""

    channel: StrictStr
    template_ref: StrictStr

    @field_validator("template_ref")
    @classmethod
    def _check_template_ref_is_a_release_tag(cls, value: str) -> str:
        stripped = value.strip()
        if _TEMPLATE_TAG_RE.match(stripped) is None:
            raise ValueError("must be a minds-vX.Y.Z release tag (web creates lease an exact match on it)")
        return stripped


def template_version_of(template_ref: str) -> str:
    """The plain ``X.Y.Z`` a ``minds-vX.Y.Z`` tag names."""
    match = _TEMPLATE_TAG_RE.match(template_ref)
    if match is None:
        raise PromotionError(f"{template_ref!r} is not a minds-vX.Y.Z release tag.")
    return match.group(1)


def parse_web_channels(text: str) -> tuple[WebChannelEntry, ...]:
    """Read the declared web channels, rejecting anything malformed before any network call.

    Reads only the ``[web_channels]`` table; ``publish.parse_channels`` owns the
    rest of the file (and is what refuses unknown top-level keys).
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PromotionError(f"release-channels.toml is not valid TOML: {exc}") from exc
    declared = raw.get(WEB_CHANNELS_TABLE, {})
    if not isinstance(declared, dict):
        raise PromotionError(f"`{WEB_CHANNELS_TABLE}` must be a table of per-channel entries, not {declared!r}.")
    unknown = sorted(set(declared) - set(PUBLISHABLE_CHANNELS))
    if unknown:
        raise PromotionError(
            f"Unknown web channel(s) {unknown}. Only {list(PUBLISHABLE_CHANNELS)} are served from a manifest."
        )
    entries = []
    for channel in PUBLISHABLE_CHANNELS:
        if channel not in declared:
            continue
        fields = declared[channel]
        if not isinstance(fields, dict):
            raise PromotionError(
                f"[{WEB_CHANNELS_TABLE}.{channel}] must be a table declaring `template_ref`, not {fields!r}."
            )
        if "channel" in fields:
            raise PromotionError(
                f"[{WEB_CHANNELS_TABLE}.{channel}] declares `channel`, which names the table rather than a field."
            )
        try:
            entries.append(WebChannelEntry(channel=channel, **fields))
        except ValidationError as exc:
            faults = "; ".join(f"{'.'.join(str(p) for p in e['loc'])} {e['msg'].lower()}" for e in exc.errors())
            raise PromotionError(f"[{WEB_CHANNELS_TABLE}.{channel}] {faults}.") from exc
    return tuple(entries)


def web_channel_filename(channel: str) -> str:
    """The object the connector reads a web channel's pin from, e.g. ``stable-web.json``."""
    return f"{channel}-{WEB_PLATFORM}.json"


def render_web_manifest(entry: WebChannelEntry) -> Manifest:
    """The document the connector reads: the tag, plus the version it names for readers of the feed."""
    return {"version": template_version_of(entry.template_ref), TEMPLATE_REF_KEY: entry.template_ref}


def render_web_manifest_text(manifest: Manifest) -> str:
    """The document as the connector reads it (and as the bucket stores it)."""
    return json.dumps(dict(manifest), indent=2) + "\n"


def parse_web_manifest(manifest_text: str, source: str) -> Manifest:
    """``source`` names the document so a corrupt object in the bucket is reported by location."""
    try:
        document = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"{source} is not valid JSON: {exc}.") from exc
    if not isinstance(document, dict):
        raise PromotionError(f"{source} is not a JSON object.")
    if TEMPLATE_REF_KEY not in document:
        raise PromotionError(f"{source} has no `{TEMPLATE_REF_KEY}` key.")
    return document


ListRemoteTags = Callable[[str], frozenset[str]]


@functools.cache
def list_remote_template_tags(remote: str) -> frozenset[str]:
    """The tag names on ``remote`` per ``git ls-remote --tags`` (annotated tags' peeled refs dropped).

    Cached per remote: every web entry gates on the same listing, and this
    is a one-shot CLI, so one run makes one round trip.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", remote],
            check=True,
            capture_output=True,
            text=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PromotionError(f"Cannot list the tags on {remote}: {exc}.") from exc
    tags = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.add(ref.removeprefix("refs/tags/"))
    return frozenset(tags)


def assert_template_tag_exists(entry: WebChannelEntry, list_remote_tags: ListRemoteTags) -> None:
    """A web pin nobody can lease is refused: the tag must exist on the template remote.

    Mirrors the pool bake's ``--from-tag`` check. A typo here would otherwise
    publish a pin that matches no baked row, and every web create would fail
    with a no-capacity error until somebody noticed.
    """
    if entry.template_ref not in list_remote_tags(DEFAULT_WORKSPACE_TEMPLATE_REMOTE):
        raise PromotionError(
            f"[{WEB_CHANNELS_TABLE}.{entry.channel}] names {entry.template_ref}, which is not a tag on "
            f"{DEFAULT_WORKSPACE_TEMPLATE_REMOTE}. Web creates lease only pool hosts baked at that exact tag."
        )


def _read_web_manifest_from_feed(feed_base_url: str, channel: str, fetch: Fetch) -> Manifest | None:
    url = f"{feed_base_url.rstrip('/')}/{web_channel_filename(channel)}"
    try:
        text = fetch(url).decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PromotionError(f"Cannot read the current {channel} web pin: {url} returned {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise PromotionError(f"Cannot reach {url} to read the current {channel} web pin: {exc.reason}.") from exc
    return parse_web_manifest(text, url)


def _read_web_manifest_from_bucket(bucket: str, channel: str, make_client: MakeS3Client) -> Manifest | None:
    client = make_client()
    key = web_channel_filename(channel)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except ClientError as exc:
        raise PromotionError(f"Cannot read the current {channel} web pin from {bucket}/{key}: {exc}.") from exc
    return parse_web_manifest(response["Body"].read().decode("utf-8"), f"{bucket}/{key}")


def read_current_web_manifest(
    channel: str,
    *,
    bucket: str,
    feed_base_url: str,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
) -> Manifest | None:
    """What a web channel pins now, or None when it has never been published.

    Same two sources, same rule, as the desktop channels: the bucket when a
    credential is held (read-after-write consistent), the CDN-cached feed for
    the credential-less validate job.
    """
    if from_bucket:
        return _read_web_manifest_from_bucket(bucket, channel, make_client)
    return _read_web_manifest_from_feed(feed_base_url, channel, fetch)


def upload_web_manifest(
    manifest: Manifest, *, bucket: str, channel: str, cache_seconds: int, make_client: MakeS3Client = r2_client
) -> str:
    """Write one web channel's manifest to R2 with the same short TTL as the desktop manifests."""
    key = web_channel_filename(channel)
    make_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=render_web_manifest_text(manifest).encode("utf-8"),
        ContentType="application/json",
        CacheControl=f"public, max-age={cache_seconds}",
    )
    return key


def _describe_served_web_pin(current: Manifest | None) -> str:
    if current is None:
        return "nothing"
    return str(current.get(TEMPLATE_REF_KEY, f"a manifest naming no {TEMPLATE_REF_KEY}"))


def apply_web_entry(
    entry: WebChannelEntry,
    *,
    bucket: str,
    feed_base_url: str,
    cache_seconds: int,
    dry_run: bool,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
    list_remote_tags: ListRemoteTags = list_remote_template_tags,
) -> str:
    """Run the web gate for one channel, then publish its pin unless this is a dry run."""
    assert_template_tag_exists(entry, list_remote_tags)
    manifest = render_web_manifest(entry)
    current = read_current_web_manifest(
        entry.channel,
        bucket=bucket,
        feed_base_url=feed_base_url,
        from_bucket=from_bucket,
        fetch=fetch,
        make_client=make_client,
    )
    label = f"{entry.channel} ({WEB_PLATFORM})"
    served_description = _describe_served_web_pin(current)
    if current is not None and current == manifest:
        return f"{label}: already pinning web creates to {entry.template_ref}, nothing to do"
    if dry_run:
        return f"{label}: would pin web creates to {entry.template_ref} (currently {served_description})"
    upload_web_manifest(
        manifest, bucket=bucket, channel=entry.channel, cache_seconds=cache_seconds, make_client=make_client
    )
    return f"{label}: pinned web creates to {entry.template_ref} (was {served_description})"


def undeclared_web_channel_reports(
    entries: tuple[WebChannelEntry, ...],
    *,
    bucket: str,
    feed_base_url: str,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
) -> tuple[str, ...]:
    """Name every web channel still served by a manifest this file no longer declares.

    Removing an entry publishes nothing, so the connector keeps reading the
    last pin -- the same rule as the desktop channels.
    """
    declared = {entry.channel for entry in entries}
    reports = []
    for channel in PUBLISHABLE_CHANNELS:
        if channel in declared:
            continue
        current = read_current_web_manifest(
            channel,
            bucket=bucket,
            feed_base_url=feed_base_url,
            from_bucket=from_bucket,
            fetch=fetch,
            make_client=make_client,
        )
        if current is not None:
            reports.append(
                f"{channel} ({WEB_PLATFORM}): declared by no entry, but web creates still pin to "
                f"{_describe_served_web_pin(current)}. Removing an entry withdraws nothing; repoint it to move the pin."
            )
    return tuple(reports)
