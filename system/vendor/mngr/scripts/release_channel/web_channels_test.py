import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber

from scripts.release_channel.manifest import PUBLISHABLE_CHANNELS
from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.publish import parse_channels
from scripts.release_channel.web_channels import TEMPLATE_REF_KEY
from scripts.release_channel.web_channels import WebChannelEntry
from scripts.release_channel.web_channels import apply_web_entry
from scripts.release_channel.web_channels import parse_web_channels
from scripts.release_channel.web_channels import render_web_manifest
from scripts.release_channel.web_channels import render_web_manifest_text
from scripts.release_channel.web_channels import undeclared_web_channel_reports
from scripts.release_channel.web_channels import web_channel_filename

FEED = "https://updates.example.com"
_REPO_ROOT = Path(__file__).resolve().parents[2]

ALPHA = WebChannelEntry(channel="alpha", template_ref="minds-v0.6.1")
PUBLISHED_ALPHA = render_web_manifest_text(render_web_manifest(ALPHA))


def _not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "not found", Message(), BytesIO(b""))


def _fetch(served: str | None):
    """Serve the channel's current web pin from the feed, or a 404 when never published."""

    def fetch(url: str) -> bytes:
        if url.endswith("-web.json"):
            if served is None:
                raise _not_found(url)
            return served.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _tags_on_remote(*tags: str):
    def list_remote_tags(remote: str) -> frozenset[str]:
        assert "default-workspace-template" in remote
        return frozenset(tags)

    return list_remote_tags


def _no_client() -> Any:
    raise AssertionError("built an S3 client for a run that holds no credential")


def _apply(entry: WebChannelEntry, *, served: str | None, tags: tuple[str, ...] = ("minds-v0.6.1",), **overrides):
    kwargs: dict[str, Any] = {
        "bucket": "bucket",
        "feed_base_url": FEED,
        "cache_seconds": 60,
        "dry_run": True,
        "from_bucket": False,
        "fetch": _fetch(served),
        "make_client": _no_client,
        "list_remote_tags": _tags_on_remote(*tags),
    }
    kwargs.update(overrides)
    return apply_web_entry(entry, **kwargs)


def test_the_shipped_file_declares_a_web_pin_for_every_desktop_channel() -> None:
    text = (_REPO_ROOT / "apps" / "minds" / "release-channels.toml").read_text()
    desktop_channels = {entry.channel for entry in parse_channels(text)}
    web_channels = {entry.channel for entry in parse_web_channels(text)}
    assert web_channels == desktop_channels == set(PUBLISHABLE_CHANNELS)


def test_the_web_table_is_not_a_stray_top_level_key() -> None:
    text = '[web_channels.stable]\ntemplate_ref = "minds-v0.6.0"\n'
    assert parse_channels(text) == ()
    assert parse_web_channels(text) == (WebChannelEntry(channel="stable", template_ref="minds-v0.6.0"),)


def test_a_file_without_the_web_table_declares_no_web_pins() -> None:
    assert parse_web_channels("") == ()


def test_a_web_pin_that_is_not_a_release_tag_is_refused_by_name() -> None:
    with pytest.raises(PromotionError, match=r"(?i)\[web_channels\.alpha\] template_ref .*minds-vx\.y\.z"):
        parse_web_channels('[web_channels.alpha]\ntemplate_ref = "main"\n')


def test_a_web_pin_with_an_unknown_field_is_refused() -> None:
    with pytest.raises(PromotionError, match=r"\[web_channels\.alpha\] rollout_percentage"):
        parse_web_channels('[web_channels.alpha]\ntemplate_ref = "minds-v0.6.0"\nrollout_percentage = 50\n')


def test_an_unknown_web_channel_is_refused() -> None:
    with pytest.raises(PromotionError, match="Unknown web channel"):
        parse_web_channels('[web_channels.nightly]\ntemplate_ref = "minds-v0.6.0"\n')


def test_a_web_entry_that_is_not_a_table_is_refused_by_name() -> None:
    with pytest.raises(PromotionError, match=r"\[web_channels\.alpha\] must be a table"):
        parse_web_channels('[web_channels]\nalpha = "minds-v0.6.0"\n')


def test_the_rendered_manifest_carries_the_tag_and_its_version() -> None:
    assert render_web_manifest(ALPHA) == {"version": "0.6.1", TEMPLATE_REF_KEY: "minds-v0.6.1"}


def test_the_web_channel_file_sits_beside_the_desktop_ones() -> None:
    assert web_channel_filename("stable") == "stable-web.json"


def test_a_tag_missing_from_the_template_remote_is_refused_before_any_read() -> None:
    def refusing_fetch(url: str) -> bytes:
        raise AssertionError(f"read {url} before the tag gate")

    with pytest.raises(PromotionError, match="minds-v0.6.1, which is not a tag"):
        _apply(ALPHA, served=None, tags=("minds-v0.6.0",), fetch=refusing_fetch)


def test_a_dry_run_reports_the_move_without_publishing() -> None:
    older = render_web_manifest_text(
        render_web_manifest(WebChannelEntry(channel="alpha", template_ref="minds-v0.6.0"))
    )
    assert _apply(ALPHA, served=older) == (
        "alpha (web): would pin web creates to minds-v0.6.1 (currently minds-v0.6.0)"
    )


def test_a_never_published_web_channel_is_a_first_publish() -> None:
    assert "currently nothing" in _apply(ALPHA, served=None)


def test_a_web_channel_already_at_the_declared_tag_is_a_no_op() -> None:
    assert "already pinning web creates to minds-v0.6.1" in _apply(ALPHA, served=PUBLISHED_ALPHA)


def test_a_real_run_uploads_the_web_manifest_under_the_channel_key(stub_s3_client: Any) -> None:
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "bucket",
                "Key": "alpha-web.json",
                "Body": PUBLISHED_ALPHA.encode("utf-8"),
                "ContentType": "application/json",
                "CacheControl": "public, max-age=60",
            },
        )
        result = _apply(ALPHA, served=None, dry_run=False, make_client=lambda: client)
        stubber.assert_no_pending_responses()
    assert result == "alpha (web): pinned web creates to minds-v0.6.1 (was nothing)"


def test_a_credentialed_run_reads_the_bucket_not_the_feed(stub_s3_client: Any) -> None:
    def refusing_feed_fetch(url: str) -> bytes:
        raise AssertionError("the feed was read on a run that says it reads the bucket")

    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": BytesIO(PUBLISHED_ALPHA.encode("utf-8"))},
            expected_params={"Bucket": "bucket", "Key": "alpha-web.json"},
        )
        result = _apply(ALPHA, served=None, from_bucket=True, fetch=refusing_feed_fetch, make_client=lambda: client)
        stubber.assert_no_pending_responses()
    assert "already pinning" in result


def test_a_web_channel_the_file_no_longer_declares_is_reported_as_still_pinning() -> None:
    reports = undeclared_web_channel_reports(
        (ALPHA,), bucket="bucket", feed_base_url=FEED, from_bucket=False, fetch=_fetch(PUBLISHED_ALPHA)
    )
    assert len(reports) == 2
    assert all("declared by no entry, but web creates still pin to minds-v0.6.1" in report for report in reports)
    assert not any(report.startswith("alpha") for report in reports)


def test_a_web_channel_nobody_has_ever_published_to_is_not_reported() -> None:
    assert (
        undeclared_web_channel_reports(
            (ALPHA,), bucket="bucket", feed_base_url=FEED, from_bucket=False, fetch=_fetch(None)
        )
        == ()
    )
