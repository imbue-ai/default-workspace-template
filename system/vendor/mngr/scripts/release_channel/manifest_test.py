import json
import urllib.error
from email.message import Message
from io import BytesIO
from typing import Any

import pytest
import yaml
from botocore.stub import Stubber

from scripts.release_channel.manifest import Manifest
from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.manifest import assert_lima_image_published
from scripts.release_channel.manifest import assert_plain_release_version
from scripts.release_channel.manifest import fetch_build_manifest
from scripts.release_channel.manifest import is_a_version_decrease
from scripts.release_channel.manifest import parse_manifest
from scripts.release_channel.manifest import read_channel_manifest_from_bucket
from scripts.release_channel.manifest import read_channel_manifest_from_feed
from scripts.release_channel.manifest import read_rollout_percentage
from scripts.release_channel.manifest import render
from scripts.release_channel.manifest import rewrite_manifest
from scripts.release_channel.manifest import upload_manifest
from scripts.release_channel.manifest import version_of
from scripts.release_channel.manifest import with_rollout_percentage

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


def _rolled_out_at(percentage: int) -> Manifest:
    """The real build's channel manifest, declaring `percentage`."""
    return with_rollout_percentage(rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID), percentage)


def test_rewrite_makes_every_artifact_absolute_to_todesktop() -> None:
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    urls = [line.split(": ", 1)[1] for line in render(manifest).splitlines() if ": http" in line]
    assert len(urls) == 5, "four files plus the legacy top-level path"
    assert all(url.startswith(f"https://download.todesktop.com/{APP_ID}/") for url in urls)


def test_rewrite_preserves_everything_but_the_artifact_references() -> None:
    """The whole point is that the promoted artifacts are the ones ToDesktop signed."""
    original = yaml.safe_load(REAL_TODESKTOP_MANIFEST)
    published = yaml.safe_load(render(rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)))
    assert published.keys() == original.keys(), "a key ToDesktop set was dropped"
    assert published["version"] == original["version"]
    assert published["releaseDate"] == original["releaseDate"]
    # Paired and ordered, so a digest cannot end up against the wrong artifact.
    assert [(f["sha512"], f["size"]) for f in published["files"]] == [
        (f["sha512"], f["size"]) for f in original["files"]
    ]


def test_rewrite_percent_encodes_spaces_so_the_url_resolves() -> None:
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    assert "Minds%200.3.11%20-%20Build%20260801n4rh5zv5d-arm64-mac.zip" in render(manifest)
    assert "Minds 0.3.11 - Build" not in render(manifest)


def test_rewrite_keeps_the_extension_electron_updater_selects_on() -> None:
    """MacUpdater picks the zip by URL pathname extension, so it must survive."""
    manifest = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    zips = [line for line in render(manifest).splitlines() if line.strip().endswith("-mac.zip")]
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
    assert "https://cdn.example/Minds-arm64-mac.zip" in render(rewrite_manifest(manifest, APP_ID))


def test_rewrite_rejects_a_manifest_with_no_artifacts() -> None:
    with pytest.raises(PromotionError, match="no artifacts"):
        rewrite_manifest("version: 0.4.0\nfiles:\n", APP_ID)


def test_parsing_reads_the_version_from_the_real_manifest() -> None:
    assert version_of(parse_manifest(REAL_TODESKTOP_MANIFEST, "ToDesktop's build manifest")) == "0.3.11"


def test_a_manifest_with_no_version_names_which_manifest_it_was() -> None:
    """Otherwise "pick another build" and "our bucket holds junk" read identically."""
    with pytest.raises(PromotionError, match="ToDesktop's build manifest has no"):
        rewrite_manifest("files:\n  - url: Minds-arm64-mac.zip\n", APP_ID)
    with pytest.raises(PromotionError, match="https://releases.test/alpha-mac.yml has no"):
        read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_serving(b"files:\n"))


def test_a_document_that_is_not_a_manifest_at_all_names_which_one_it_was() -> None:
    """The shape a CDN or bucket error page arrives in, which is not a missing field.

    An error page served with a 200 loads as a scalar rather than a mapping, and
    a truncated object fails the loader outright -- so neither can be read past
    into a `manifest["version"]`.
    """
    with pytest.raises(PromotionError, match="ToDesktop's build manifest is not valid YAML"):
        rewrite_manifest("version: [0.4.0\n", APP_ID)
    with pytest.raises(PromotionError, match="https://releases.test/alpha-mac.yml is not a YAML mapping"):
        read_channel_manifest_from_feed(
            "https://releases.test", "alpha", fetch=_serving(b"<html>404 Not Found</html>\n")
        )


def test_fetch_build_manifest_names_the_build_when_it_is_missing() -> None:
    with pytest.raises(PromotionError, match="No ToDesktop manifest for build nope"):
        fetch_build_manifest(APP_ID, "nope", fetch=_raising(404))


def test_a_channel_that_was_never_published_reads_as_none() -> None:
    assert read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_raising(404)) is None


def test_reading_a_channel_yields_the_whole_manifest_not_just_its_version() -> None:
    """Two builds share a version between cuts, so only the whole document tells them apart."""
    served = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    current = read_channel_manifest_from_feed(
        "https://releases.test", "alpha", fetch=_serving(render(served).encode())
    )
    assert current == served


def test_reading_a_channel_propagates_errors_that_are_not_absence() -> None:
    """A 503 must not be mistaken for "never published" and silently overwritten."""
    with pytest.raises(PromotionError, match="returned 503"):
        read_channel_manifest_from_feed("https://releases.test", "alpha", fetch=_raising(503))


def test_moving_forward_or_standing_still_is_not_a_decrease() -> None:
    assert not is_a_version_decrease("0.4.12", "0.4.13")
    assert not is_a_version_decrease("0.4.12", "0.4.12")
    assert not is_a_version_decrease(None, "0.4.12")


def test_moving_a_channel_backwards_is_reported_rather_than_refused() -> None:
    """Nobody is pulled back, so this only changes what a new download gets."""
    assert is_a_version_decrease("0.4.12", "0.4.11")


def test_a_prerelease_version_is_rejected() -> None:
    """Versions are stamped once at cut, so promotion is a pointer move.

    A prerelease suffix means the promoted build would have to be rebuilt under a
    new version -- and then the bytes that soaked are not the bytes that ship.
    """
    with pytest.raises(PromotionError, match="plain X.Y.Z"):
        assert_plain_release_version("0.5.0-alpha.1")
    assert_plain_release_version("0.4.12")


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
                "Body": render(manifest).encode("utf-8"),
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
    """What a channel serves, taken from the object itself.

    Through the feed it is served with a max-age, so a promotion run inside that
    window reads the previous manifest back and reports a version the channel has
    already left.
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
    assert version_of(current) == "0.4.12"
    assert render(current) == "version: 0.4.12\n"


def test_a_channel_with_no_object_yet_reads_as_never_published(stub_s3_client: Any) -> None:
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        assert read_channel_manifest_from_bucket("bucket", "beta", make_client=lambda: client) is None


def test_a_bucket_read_that_is_not_a_missing_object_refuses_the_promotion(stub_s3_client: Any) -> None:
    """Absent is the only reading of "nothing published yet".

    A denied read or a throttle leaves the current version unknown, which is the
    one state a promotion must not proceed from -- taking it as "never
    published" would report a first publish over a channel it cannot see.
    """
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(PromotionError, match="Cannot read the current alpha version"):
            read_channel_manifest_from_bucket("bucket", "alpha", make_client=lambda: client)


def test_the_rollout_is_declared_where_electron_updater_reads_it() -> None:
    """Top level, and parseable -- it reads `updateInfo.stagingPercentage` off the parsed document."""
    published = _rolled_out_at(10)
    assert yaml.safe_load(render(published))["stagingPercentage"] == 10


def test_declaring_a_rollout_leaves_the_artifacts_untouched() -> None:
    """The bytes a channel serves stay the ones ToDesktop signed."""
    rewritten = rewrite_manifest(REAL_TODESKTOP_MANIFEST, APP_ID)
    published = with_rollout_percentage(rewritten, 30)
    assert yaml.safe_load(render(published))["files"] == yaml.safe_load(render(rewritten))["files"]
    assert version_of(published) == version_of(rewritten)


# Every spelling js-yaml reads as the same key.
@pytest.mark.parametrize("declared", ["stagingPercentage: 99", '"stagingPercentage": 99', "stagingPercentage : 99"])
def test_a_rollout_arriving_from_upstream_is_replaced_rather_than_joined(declared: str) -> None:
    """Two keys is not a merge, it is an unparseable document.

    js-yaml refuses a duplicated mapping key, and electron-updater turns that
    into a failed check for every install on the channel -- so a stray key in
    ToDesktop's manifest would take the channel down rather than be ignored.
    """
    published = with_rollout_percentage(rewrite_manifest(f"{declared}\n" + REAL_TODESKTOP_MANIFEST, APP_ID), 10)
    assert render(published).count("stagingPercentage") == 1
    assert yaml.safe_load(render(published))["stagingPercentage"] == 10


def test_a_manifest_declaring_no_rollout_reads_as_none() -> None:
    """What every manifest published before rollouts existed looks like."""
    assert read_rollout_percentage(parse_manifest(REAL_TODESKTOP_MANIFEST, "x"), "x") is None


# Every spelling of null, which js-yaml reads the same way as pyyaml does.
@pytest.mark.parametrize("declared", ["null", "~", ""])
def test_a_rollout_declared_as_null_reads_as_none_rather_than_being_refused(declared: str) -> None:
    """electron-updater skips staging for a null exactly as it does for no key.

    So refusing it would stop a promotion -- including the operator lowering a
    percentage to halt a bad build -- over a manifest the client reads as
    declaring no rollout at all.
    """
    assert (
        read_rollout_percentage(parse_manifest(f"version: 0.3.11\nstagingPercentage: {declared}\n", "x"), "x") is None
    )


@pytest.mark.parametrize("declared", ["ten", "99.9", "٣", "²"])
def test_a_published_manifest_with_a_non_numeric_rollout_is_refused(declared: str) -> None:
    """electron-updater rolls a NaN out to everyone and truncates a float, so neither can be read past."""
    with pytest.raises(PromotionError, match="input should be a valid integer"):
        read_rollout_percentage(parse_manifest(f"version: 0.3.11\nstagingPercentage: {declared}\n", "x"), "x")


# pyyaml is YAML 1.1, where a leading zero is octal and `1:30` is sexagesimal.
# The client's js-yaml is 1.2 and reads neither that way.
@pytest.mark.parametrize(
    ("declared", "read_as", "client_reads"),
    [("010", 8, 10), ("017", 15, 17), ("050", 40, 50), ("1:30", 90, "1:30")],
)
def test_a_spelling_this_tool_never_writes_is_read_as_yaml_1_1(
    declared: str, read_as: int, client_reads: object
) -> None:
    """The known gap, pinned so that closing it would be a deliberate act.

    Only this tool writes the key and it writes plain decimal, so a value in one
    of these shapes means the object in the bucket was hand-edited. What it costs
    is the report line: nothing gates on the served rollout, so the run names a
    percentage no client honoured and publishes anyway. Closing it costs either a
    second YAML library or a hand-written 1.2 loader.
    """
    assert (
        read_rollout_percentage(parse_manifest(f"version: 0.3.11\nstagingPercentage: {declared}\n", "x"), "x")
        == read_as
    )
    assert read_as != client_reads


@pytest.mark.parametrize("declared", ["-5", "150"])
def test_a_published_manifest_with_an_out_of_range_rollout_is_refused_as_such(declared: str) -> None:
    """A number, and refused too -- but named as the range failure it is.

    Only this tool writes the key, and it range-checks, so a value out here means
    the object was hand-edited rather than that the channel is at -5%.
    """
    with pytest.raises(PromotionError, match="input should be (less|greater) than or equal to"):
        read_rollout_percentage(parse_manifest(f"version: 0.3.11\nstagingPercentage: {declared}\n", "x"), "x")
