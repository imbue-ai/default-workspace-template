#!/usr/bin/env python3
"""Publish every channel manifest declared in ``apps/minds/release-channels.toml``.

The file is the desired state and this makes it so, which is what turns a
promotion into a reviewable pull request: CI dry-runs this on the PR, so a
reviewer sees whether the change would actually publish, and merging is what
applies it.

A channel is moved by repointing its entry, never by removing it: nothing here
deletes an object, so a channel whose entry is gone keeps serving whatever it
last published. That divergence is reported rather than corrected -- dropping
``<channel>-mac.yml`` would leave every client on that channel erroring against a
feed that serves nothing.

``manifest.py`` stays the single-channel primitive; this reads the file, checks
each entry against reality, and calls it.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import click

from scripts.r2.client import R2CredentialsError
from scripts.r2.client import has_r2_credentials
from scripts.release_channel.manifest import Fetch
from scripts.release_channel.manifest import MakeS3Client
from scripts.release_channel.manifest import PUBLISHABLE_CHANNELS
from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.manifest import assert_lima_image_published
from scripts.release_channel.manifest import assert_not_a_rollback
from scripts.release_channel.manifest import fetch_build_manifest
from scripts.release_channel.manifest import http_get
from scripts.release_channel.manifest import r2_client
from scripts.release_channel.manifest import read_current_channel_manifest
from scripts.release_channel.manifest import rewrite_manifest
from scripts.release_channel.manifest import upload_manifest

_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("build_id", "version", "fallback_branch")


@dataclass(frozen=True)
class ChannelEntry:
    """One channel's declared state."""

    channel: str
    build_id: str
    version: str
    fallback_branch: str


def parse_channels(text: str) -> tuple[ChannelEntry, ...]:
    """Read the declared channels, rejecting anything malformed before any network call."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PromotionError(f"release-channels.toml is not valid TOML: {exc}") from exc

    # Shape-checked before anything is read out of it. TOML admits any value in
    # either place, and `main` catches only the errors raised here, so an entry that is
    # not a table reaches the promote job's output as a traceback rather than as
    # the one thing every other refusal here gives: the offending key by name.
    declared = raw.get("channels", {})
    if not isinstance(declared, dict):
        raise PromotionError(f"`channels` must be a table of per-channel entries, not {declared!r}.")
    unknown = sorted(set(declared) - set(PUBLISHABLE_CHANNELS))
    if unknown:
        raise PromotionError(
            f"Unknown channel(s) {unknown}. Only {list(PUBLISHABLE_CHANNELS)} are served from a manifest."
        )
    entries = []
    for channel in PUBLISHABLE_CHANNELS:
        if channel not in declared:
            continue
        fields = declared[channel]
        if not isinstance(fields, dict):
            raise PromotionError(
                f"[channels.{channel}] must be a table declaring {list(_REQUIRED_FIELDS)}, not {fields!r}."
            )
        missing = [f for f in _REQUIRED_FIELDS if not fields.get(f)]
        if missing:
            raise PromotionError(f"[channels.{channel}] is missing {missing}.")
        entries.append(
            ChannelEntry(
                channel=channel,
                build_id=fields["build_id"],
                version=fields["version"],
                fallback_branch=fields["fallback_branch"],
            )
        )
    return tuple(entries)


def assert_version_matches_build(entry: ChannelEntry, manifest_version: str) -> None:
    """The declared version must be what the build actually is.

    The file is what a reviewer reads, so a version that does not match the
    build id would make the review meaningless -- someone would approve "move
    alpha to 0.4.2" while the build id said something else entirely.
    """
    if entry.version != manifest_version:
        raise PromotionError(
            f"[channels.{entry.channel}] says version {entry.version}, but build {entry.build_id} "
            f"is version {manifest_version}. Fix the file so the review says what it does."
        )


def assert_fallback_branch_matches_build(entry: ChannelEntry, manifest_version: str) -> None:
    """The dwt tag a build clones is ``minds-v<its version>``, so it is not free text.

    Nothing here can read the tag baked into the build, so the Lima image gate can
    only check the one this file names -- and copying the previous row's, while
    bumping the two fields beside it, makes that gate assert the PREVIOUS release's
    image and pass. Clients then ask for the tag the binary actually ships, get
    VERSION_UNAVAILABLE, and silently build in-VM.
    """
    expected = f"minds-v{manifest_version}"
    if entry.fallback_branch != expected:
        raise PromotionError(
            f"[channels.{entry.channel}] says fallback_branch {entry.fallback_branch}, but a build at "
            f"{manifest_version} clones {expected} (apps/minds/docs/deploy/release.md step 1 moves the version and "
            f"FALLBACK_BRANCH together). The Lima image gate would check the wrong tag's image."
        )


def apply_entry(
    entry: ChannelEntry,
    *,
    app_id: str,
    bucket: str,
    feed_base_url: str,
    lima_image_base_url: str | None,
    arches: tuple[str, ...],
    allow_rollback: bool,
    cache_seconds: int,
    dry_run: bool,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
) -> str:
    """Run every gate for one channel, then publish unless this is a dry run."""
    manifest = rewrite_manifest(fetch_build_manifest(app_id, entry.build_id, fetch=fetch), app_id)
    assert_version_matches_build(entry, manifest.version)
    assert_fallback_branch_matches_build(entry, manifest.version)

    if lima_image_base_url:
        assert_lima_image_published(lima_image_base_url, entry.fallback_branch, arches, fetch=fetch)

    current = read_current_channel_manifest(
        entry.channel,
        bucket=bucket,
        feed_base_url=feed_base_url,
        from_bucket=from_bucket,
        fetch=fetch,
        make_client=make_client,
    )
    served = current.version if current is not None else None
    assert_not_a_rollback(entry.channel, served, manifest.version, allow_rollback)

    # Compared whole rather than by version: a channel is declared by build id,
    # and every build between two cuts carries the version of the last cut, so
    # comparing versions would make the ordinary alpha promotion -- a new build
    # at the version already served -- report success having published nothing.
    if current is not None and current.text == manifest.text:
        return f"{entry.channel}: already serving build {entry.build_id} ({manifest.version}), nothing to do"
    if dry_run:
        return f"{entry.channel}: would publish {manifest.version} (currently {served or 'nothing'})"
    upload_manifest(
        manifest, bucket=bucket, channel=entry.channel, cache_seconds=cache_seconds, make_client=make_client
    )
    return f"{entry.channel}: published {manifest.version} (was {served or 'nothing'})"


def undeclared_channel_reports(
    entries: tuple[ChannelEntry, ...],
    *,
    bucket: str,
    feed_base_url: str,
    from_bucket: bool,
    fetch: Fetch = http_get,
    make_client: MakeS3Client = r2_client,
) -> tuple[str, ...]:
    """Name every channel still served by a manifest this file no longer declares.

    Removing an entry publishes nothing, so the channel keeps serving its last
    build -- which makes the one edit a reader would reach for to withdraw a bad
    promotion a run that reports success having changed nothing.
    """
    declared = {entry.channel for entry in entries}
    reports = []
    for channel in PUBLISHABLE_CHANNELS:
        if channel in declared:
            continue
        current = read_current_channel_manifest(
            channel,
            bucket=bucket,
            feed_base_url=feed_base_url,
            from_bucket=from_bucket,
            fetch=fetch,
            make_client=make_client,
        )
        if current is not None:
            reports.append(
                f"{channel}: declared by no entry, but still serving {current.version}. Removing an entry "
                f"withdraws nothing; repoint it at another build to move the channel."
            )
    return tuple(reports)


@click.command()
@click.option(
    "--channels-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("apps/minds/release-channels.toml"),
    show_default=True,
)
@click.option("--app-id", required=True, help="ToDesktop app id, e.g. 26032588hqdzk")
@click.option("--bucket", required=True, help="R2 bucket holding the channel manifests")
@click.option("--feed-base-url", required=True, help="Public URL the bucket is served at")
@click.option("--lima-image-base-url", default=None, help="Image chunk store to gate on; omit if the tier has none")
@click.option("--arch", "arches", multiple=True, default=("aarch64",), help="Arches that must have an image")
@click.option("--allow-rollback", is_flag=True, help="Permit moving a channel to an older version")
@click.option("--cache-seconds", default=60, show_default=True, help="Cache-Control max-age on the manifest")
@click.option("--dry-run", is_flag=True, help="Run every gate and report, but publish nothing")
def main(
    channels_file: Path,
    app_id: str,
    bucket: str,
    feed_base_url: str,
    lima_image_base_url: str | None,
    arches: tuple[str, ...],
    allow_rollback: bool,
    cache_seconds: int,
    dry_run: bool,
) -> None:
    try:
        entries = parse_channels(channels_file.read_text())
        # Said out loud because the two sources can disagree: a credential-less
        # dry run reads the feed, and the publish that follows it reads the
        # bucket.
        from_bucket = has_r2_credentials(os.environ)
        click.echo(
            f"Reading current channel state from the bucket {bucket}."
            if from_bucket
            else f"No R2 credentials: reading current channel state from {feed_base_url}, which the CDN may cache."
        )
        if not entries:
            click.echo("No channels declared; nothing to publish.")
        if entries and not lima_image_base_url:
            # Said out loud because release-channels.toml promises a reviewer
            # that fallback_branch is checked against a published image, and a
            # tier with no image store is a supported configuration rather than
            # an error -- so the gate's absence has to be visible in the run.
            click.echo("No --lima-image-base-url given: this tier configures no image, so the image gate is skipped.")
        for entry in entries:
            click.echo(
                apply_entry(
                    entry,
                    app_id=app_id,
                    bucket=bucket,
                    feed_base_url=feed_base_url,
                    lima_image_base_url=lima_image_base_url,
                    arches=arches,
                    allow_rollback=allow_rollback,
                    cache_seconds=cache_seconds,
                    dry_run=dry_run,
                    from_bucket=from_bucket,
                )
            )
        for report in undeclared_channel_reports(
            entries, bucket=bucket, feed_base_url=feed_base_url, from_bucket=from_bucket
        ):
            click.echo(report)
    except (PromotionError, R2CredentialsError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
