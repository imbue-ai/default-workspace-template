import json
import urllib.error
from email.message import Message
from io import BytesIO
from typing import Any

import pytest
from botocore.stub import Stubber

from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.manifest import assert_lima_image_published
from scripts.release_channel.manifest import assert_not_a_rollback
from scripts.release_channel.manifest import fetch_build_manifest
from scripts.release_channel.manifest import parse_version
from scripts.release_channel.manifest import read_channel_manifest_from_bucket
from scripts.release_channel.manifest import read_channel_manifest_from_feed
from scripts.release_channel.manifest import rewrite_manifest
from scripts.release_channel.manifest import upload_manifest

APP_ID = "26032588hqdzk"

# Captured verbatim from
# https://download.todesktop.com/26032588hqdzk/latest-mac-build-260801n4rh5zv5d.yml
# on 2026-08-11. The rewrite has to survive the real shape -- spaces in
# filenames, a legacy top-level `path:`, and a quoted `releaseDate:` -- not a
# tidied-up approximation of it.
REAL_TODESKTOP_MANIFEST = """version: 0.3.11
files:
  - url: Minds 0.3.11 - Build 260801n4rh5zv5d-x64-mac.zip
    sha512: C18kWP7oSh2dGEymZu5Gc+zKY3ig99jsS0uD5egaigUcFzJ+0y/OjkKHXFr/VZ1QS8+sUr2jnO34VCnaX4J5nQ==
    size: 421110450
  - url: Minds 0.3.11 - Build 260801n4rh5zv5d-arm64-mac.zip
    sha512: 9Wb7wk08Qh5TW/MgHpai+doBkFg1axveQ0+dbDctYOGizgobp2r2+S45BPUoqHqyVjW0QAirCsxRiNTIOftDpQ==
    size: 415277939
  - url: Minds 0.3.11 - Build 260801n4rh5zv5d-x64.dmg
    sha512: Kx+sibg0GksyxcglDB1F50bl14uxUGciuAPLlCGj8CgEmiahvC+4lbKkeWWUm5LVnCzvlhtv7ZA9ss1uiJ/Ejg==
    size: 431215082
  - url: Minds 0.3.11 - Build 260801n4rh5zv5d-arm64.dmg
    sha512: gxvKAD+hbps5Yhs0HtszV5zHLot/v86BREQgJ6PuhQFw/Hnh9DdBQI3CZ2A21y1C9UaRsegjefgM742cquVu3Q==
    size: 425397776
path: Minds 0.3.11 - Build 260801n4rh5zv5d-x64-mac.zip
sha512: C18kWP7oSh2dGEymZu5Gc+zKY3ig99jsS0uD5egaigUcFzJ+0y/OjkKHXFr/VZ1QS8+sUr2jnO34VCnaX4J5nQ==
releaseDate: '2026-08-01T08:02:55.181Z'
"""


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.test", code, "boom", Message(), BytesIO(b""))


def _serving(body: bytes):
    return lambda _url: body


def _raising(code: int):
    def fetch(_url: str) -> bytes:
        raise _http_error(code)

    return fetch


def test_rewrite_makes_every_artifact_absolute_to_todesktop() -> None:
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    urls = [line.split(": ", 1)[1] for line in manifest.text.splitlines() if ": http" in line]
    assert len(urls) == 5, "four files plus the legacy top-level path"
    assert all(url.startswith(f"https://download.todesktop.com/{APP_ID}/") for url in urls)


def test_rewrite_preserves_digests_and_sizes_byte_for_byte() -> None:
    """The whole point is that the promoted bytes are the bytes ToDesktop signed."""
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    for line in REAL_TODESKTOP_MANIFEST.splitlines():
        if line.strip().startswith(("sha512:", "size:", "releaseDate:", "version:")):
            assert line in manifest.text.splitlines()


def test_rewrite_percent_encodes_spaces_so_the_url_resolves() -> None:
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    assert "Minds%200.3.11%20-%20Build%20260801n4rh5zv5d-arm64-mac.zip" in manifest.text
    assert "Minds 0.3.11 - Build" not in manifest.text


def test_rewrite_keeps_the_extension_electron_updater_selects_on() -> None:
    """MacUpdater picks the zip by URL pathname extension, so it must survive."""
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    zips = [line for line in manifest.text.splitlines() if line.strip().endswith("-mac.zip")]
    assert len(zips) == 3, "two per-arch zips plus the top-level path"


def test_rewrite_refuses_a_url_that_lost_its_extension() -> None:
    """The dl.todesktop.com/builds/<id>/mac/zip/<arch> form has no .zip suffix.

    electron-updater would still pick it, but only via the not-pkg-and-not-dmg
    fallback -- it stops being selected the day a fourth artifact type appears.
    """
    manifest = "version: 0.4.0\nfiles:\n  - url: https://dl.todesktop.com/app/builds/b1/mac/zip/arm64\n"
    with pytest.raises(PromotionError, match="URL extension"):
        rewrite_manifest(manifest, APP_ID)


def test_rewrite_leaves_already_absolute_urls_alone() -> None:
    manifest = "version: 0.4.0\nfiles:\n  - url: https://cdn.example/Minds-arm64-mac.zip\n"
    assert "https://cdn.example/Minds-arm64-mac.zip" in rewrite_manifest(manifest, APP_ID).text


def test_rewrite_rejects_a_manifest_with_no_artifacts() -> None:
    with pytest.raises(PromotionError, match="no artifacts"):
        rewrite_manifest("version: 0.4.0\nfiles:\n", APP_ID)


def test_parse_version_reads_the_real_manifest() -> None:
    assert parse_version(REAL_TODESKTOP_MANIFEST, "ToDesktop's build manifest") == "0.3.11"


def test_a_manifest_with_no_version_names_which_manifest_it_was() -> None:
    """Otherwise "pick another build" and "our bucket holds junk" read identically."""
    with pytest.raises(PromotionError, match="ToDesktop's build manifest has no"):
        rewrite_manifest("files:\n  - url: Minds-arm64-mac.zip\n", APP_ID)
    with pytest.raises(PromotionError, match="https://releases.test/alpha-mac.yml has no"):
        read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_serving(b"files:\n"))


def test_fetch_build_manifest_names_the_build_when_it_is_missing() -> None:
    with pytest.raises(PromotionError, match="No ToDesktop manifest for build nope"):
        fetch_build_manifest(APP_ID, "nope", fetch=_raising(404))


def test_a_channel_that_was_never_published_reads_as_none() -> None:
    assert read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_raising(404)) is None


def test_reading_a_channel_yields_the_whole_manifest_not_just_its_version() -> None:
    """Two builds share a version between cuts, so only the text tells them apart."""
    served = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    current = read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_serving(served.text.encode()))
    assert current == served


def test_reading_a_channel_propagates_errors_that_are_not_absence() -> None:
    """A 503 must not be mistaken for "never published" and silently overwritten."""
    with pytest.raises(PromotionError, match="returned 503"):
        read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_raising(503))


def test_promotion_forward_and_to_the_same_version_is_allowed() -> None:
    assert_not_a_rollback("alpha", "0.4.12", "0.4.13", allow_rollback=False)
    assert_not_a_rollback("alpha", "0.4.12", "0.4.12", allow_rollback=False)
    assert_not_a_rollback("alpha", None, "0.4.12", allow_rollback=False)


def test_moving_a_channel_backwards_needs_an_explicit_flag() -> None:
    with pytest.raises(PromotionError, match="--allow-rollback"):
        assert_not_a_rollback("alpha", "0.4.12", "0.4.11", allow_rollback=False)
    assert_not_a_rollback("alpha", "0.4.12", "0.4.11", allow_rollback=True)


def test_a_prerelease_version_is_rejected() -> None:
    """Versions are stamped once at cut, so promotion is a pointer move.

    A prerelease suffix means the promoted build would have to be rebuilt under a
    new version -- and then the bytes that soaked are not the bytes that ship.
    """
    with pytest.raises(PromotionError, match="plain X.Y.Z"):
        assert_not_a_rollback("alpha", "0.4.12", "0.5.0-alpha.1", allow_rollback=False)
    # Turning a channel on has nothing to compare against, and is the promotion
    # most likely to reach for a prerelease build.
    with pytest.raises(PromotionError, match="plain X.Y.Z"):
        assert_not_a_rollback("alpha", None, "0.5.0-alpha.1", allow_rollback=False)


def _unreachable(_url: str) -> bytes:
    raise urllib.error.URLError("nodename nor servname provided, or not known")


def test_an_unreachable_lima_image_store_blocks_the_promotion() -> None:
    """A DNS failure must not surface as a traceback, nor as a passing gate.

    An unreachable host and an unpublished image are indistinguishable from
    here, and both end with clients silently building in-VM.
    """
    with pytest.raises(PromotionError, match="Cannot reach the Lima image store"):
        assert_lima_image_published("https://nope.invalid", "minds-v0.4.17", ("aarch64",), fetch=_unreachable)


def test_an_unreachable_feed_is_not_mistaken_for_an_unpublished_channel() -> None:
    """Otherwise a network blip would read as "nothing there" and overwrite unguarded."""
    with pytest.raises(PromotionError, match="Cannot reach"):
        read_channel_manifest_from_feed("https://nope.invalid", "alpha", fetch=_unreachable)


def test_an_unreachable_todesktop_blocks_the_promotion() -> None:
    with pytest.raises(PromotionError, match="Cannot reach"):
        fetch_build_manifest(APP_ID, "b1", fetch=_unreachable)


def test_a_missing_lima_image_blocks_the_promotion() -> None:
    with pytest.raises(PromotionError, match="No Lima image published for minds-v0.4.17"):
        assert_lima_image_published("https://images.test", "minds-v0.4.17", ("aarch64",), fetch=_raising(404))


def test_a_lima_image_manifest_that_is_not_json_blocks_the_promotion() -> None:
    """A proxy error page served as 200 must refuse like every other failure here.

    Left to json, it is a JSONDecodeError traceback in the job output rather than
    a stated reason a promotion reviewer can act on.
    """
    with pytest.raises(PromotionError, match="not valid JSON"):
        assert_lima_image_published(
            "https://images.test", "minds-v0.4.17", ("aarch64",), fetch=_serving(b"<html>502</html>")
        )


def test_a_lima_image_manifest_for_a_different_tag_blocks_the_promotion() -> None:
    body = json.dumps({"minds_version": "minds-v0.4.16", "entries": [{"arch": "AARCH64"}]}).encode()
    with pytest.raises(PromotionError, match="names 'minds-v0.4.16'"):
        assert_lima_image_published("https://images.test", "minds-v0.4.17", ("aarch64",), fetch=_serving(body))


def test_a_lima_image_missing_an_arch_blocks_the_promotion() -> None:
    body = json.dumps({"minds_version": "minds-v0.4.17", "entries": [{"arch": "AARCH64"}]}).encode()
    with pytest.raises(PromotionError, match=r"missing arch\(es\) \['x86_64'\]"):
        assert_lima_image_published(
            "https://images.test", "minds-v0.4.17", ("aarch64", "x86_64"), fetch=_serving(body)
        )


def test_the_published_object_is_the_one_clients_fetch(stub_s3_client: Any) -> None:
    """The key is electron-updater's `<channel>-mac.yml`, the body is the rewritten manifest.

    Any other key publishes an object nothing ever asks for while the promotion
    still reports success. The TTL is the promotion latency, so it is pinned to
    what the caller asked for rather than to the default.
    """
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "minds-update-feed-production",
                "Key": "alpha-mac.yml",
                "Body": manifest.text.encode("utf-8"),
                "ContentType": "text/yaml",
                "CacheControl": "public, max-age=30",
            },
        )
        key = upload_manifest(
            manifest,
            bucket="minds-update-feed-production",
            channel="alpha",
            cache_seconds=30,
            make_client=lambda: client,
        )
        stubber.assert_no_pending_responses()
    assert key == "alpha-mac.yml"


def test_a_complete_lima_image_manifest_passes() -> None:
    """The arch keys are the upper-case ones the bake publishes, not the lower-case `--arch`.

    A fixture spelled the way the flag is would make this gate look like it
    passes while every real manifest reports every arch missing.
    """
    body = json.dumps(
        {"minds_version": "minds-v0.4.17", "entries": [{"arch": "AARCH64"}, {"arch": "X86_64"}]}
    ).encode()
    assert_lima_image_published("https://images.test", "minds-v0.4.17", ("aarch64", "x86_64"), fetch=_serving(body))


def test_the_current_manifest_can_be_read_from_the_bucket_rather_than_the_cdn(stub_s3_client: Any) -> None:
    """The read the rollback gate depends on, taken from the object itself.

    Through the feed it is served with a max-age, so a promotion run inside that
    window reads the previous manifest back and the gate compares against a
    version the channel has already left.
    """
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": BytesIO(b"version: 0.4.12\n")},
            expected_params={"Bucket": "minds-update-feed-production", "Key": "alpha-mac.yml"},
        )
        current = read_channel_manifest_from_bucket(
            "minds-update-feed-production", "alpha", make_client=lambda: client
        )
        stubber.assert_no_pending_responses()
    assert current is not None
    assert current.version == "0.4.12"
    assert current.text == "version: 0.4.12\n"


def test_a_channel_with_no_object_yet_reads_as_never_published(stub_s3_client: Any) -> None:
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        assert read_channel_manifest_from_bucket("bucket", "beta", make_client=lambda: client) is None


def test_a_bucket_read_that_is_not_a_missing_object_refuses_the_promotion(stub_s3_client: Any) -> None:
    """Absent is the only reading of "nothing published yet".

    A denied read or a throttle leaves the current version unknown, which is the
    one state a promotion must not proceed from -- taking it as "never
    published" would skip the rollback gate entirely.
    """
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(PromotionError, match="Cannot read the current alpha version"):
            read_channel_manifest_from_bucket("bucket", "alpha", make_client=lambda: client)
