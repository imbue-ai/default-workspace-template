import json
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber
from click.testing import CliRunner

from scripts.release_channel.manifest import FULL_ROLLOUT_PERCENTAGE
from scripts.release_channel.manifest import Fetch
from scripts.release_channel.manifest import MakeS3Client
from scripts.release_channel.manifest import PUBLISHABLE_CHANNELS
from scripts.release_channel.manifest import PromotionError
from scripts.release_channel.manifest import parse_manifest
from scripts.release_channel.manifest import read_current_channel_manifest
from scripts.release_channel.manifest import read_rollout_percentage
from scripts.release_channel.manifest import render
from scripts.release_channel.manifest import rewrite_manifest
from scripts.release_channel.manifest import version_of
from scripts.release_channel.manifest import with_rollout_percentage
from scripts.release_channel.publish import ChannelEntry
from scripts.release_channel.publish import apply_entry
from scripts.release_channel.publish import assert_version_matches_build
from scripts.release_channel.publish import main
from scripts.release_channel.publish import parse_channels
from scripts.release_channel.publish import undeclared_channel_reports

APP_ID = "26032588hqdzk"
FEED = "https://updates.example.com"

TODESKTOP_MANIFEST = """version: 0.4.12
files:
  - url: Minds 0.4.12 - Build b1-arm64-mac.zip
    sha512: abc==
    size: 1
path: Minds 0.4.12 - Build b1-arm64-mac.zip
sha512: abc==
"""

CHANNEL_MANIFEST_AT = """version: {version}
files:
  - url: https://download.todesktop.com/x/Minds-arm64-mac.zip
    sha512: abc==
    size: 1
"""

# Upper-case arch keys, which is what the bake publishes; `--arch` takes the
# lower-case spelling.
LIMA_OK = json.dumps({"minds_version": "minds-v0.4.12", "entries": [{"arch": "AARCH64"}]}).encode()


def published_b1_at(percentage: int) -> str:
    """What promoting build b1 at `percentage` writes, to serve back as current state."""
    return render(with_rollout_percentage(rewrite_manifest(TODESKTOP_MANIFEST, APP_ID), percentage))


# The state a re-run of that same promotion finds.
PUBLISHED_B1 = published_b1_at(FULL_ROLLOUT_PERCENTAGE)


def _some_other_build_at(version: str) -> str:
    """A channel manifest for a different build than b1, at `version`.

    Declares no rollout, which is what every manifest published before rollouts
    existed looks like -- so these also cover reading that state back.
    """
    return CHANNEL_MANIFEST_AT.format(version=version)


def _fetch(*, served: str | None, build: str = TODESKTOP_MANIFEST):
    """Serve ToDesktop's build manifest, the Lima manifest, and the channel's current state."""

    def fetch(url: str) -> bytes:
        if "latest-mac-build-" in url:
            return build.encode()
        if "/manifests/" in url:
            return LIMA_OK
        if url.endswith("-mac.yml"):
            if served is None:
                raise _not_found(url)
            return served.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


def _not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "not found", Message(), BytesIO(b""))


def alpha_at(percentage: int) -> ChannelEntry:
    return ChannelEntry(
        channel="alpha",
        build_id="b1",
        version="0.4.12",
        fallback_branch="minds-v0.4.12",
        rollout_percentage=percentage,
    )


ALPHA = alpha_at(FULL_ROLLOUT_PERCENTAGE)


def _feed_reads(served: str | None) -> dict:
    """Read the channel's current state from the public feed, as a credential-less run does."""
    return {"bucket": "bucket", "feed_base_url": FEED, "from_bucket": False, "fetch": _fetch(served=served)}


def _no_client() -> Any:
    """The default every `_apply` below falls back to, so a dry run proves it wrote nothing.

    Reaching the real factory would read R2 credentials out of the environment,
    which the runbook has operators export -- so a regression that published on a
    dry run would write to a bucket literally named "bucket".
    """
    raise AssertionError("built an S3 client for a run that holds no credential")


def _apply(
    entry: ChannelEntry,
    *,
    served: str | None,
    dry_run: bool = True,
    lima_image_base_url: str | None = "https://images.example.com",
    fetch: Fetch | None = None,
    from_bucket: bool = False,
    make_client: MakeS3Client = _no_client,
) -> str:
    return apply_entry(
        entry,
        app_id=APP_ID,
        bucket="bucket",
        feed_base_url=FEED,
        lima_image_base_url=lima_image_base_url,
        arches=("aarch64",),
        cache_seconds=60,
        dry_run=dry_run,
        from_bucket=from_bucket,
        fetch=fetch if fetch is not None else _fetch(served=served),
        make_client=make_client,
    )


def test_the_shipped_file_parses_and_declares_only_known_channels() -> None:
    """The real file, not a fixture -- a typo in it would break every promotion.

    Which channels it declares is state that every promotion changes, so this
    asserts the properties instead: it parses, names only publishable channels,
    and every entry it names is fully populated.
    """
    shipped = Path(__file__).parents[2] / "apps" / "minds" / "release-channels.toml"
    entries = parse_channels(shipped.read_text())
    assert entries, "the shipped file declares no channel at all"
    for entry in entries:
        assert entry.channel in PUBLISHABLE_CHANNELS
        assert entry.build_id and entry.version and entry.fallback_branch
        assert 0 <= entry.rollout_percentage <= FULL_ROLLOUT_PERCENTAGE
        # Checked here as well as in apply_entry so a stale tag fails the normal
        # test suite, not only the promote workflow.
        assert entry.fallback_branch == f"minds-v{entry.version}"


def test_stable_is_published_like_any_other_channel() -> None:
    """Stable moved onto our feed, so it is a pointer in this file like the rest."""
    entries = parse_channels(
        '[channels.stable]\nbuild_id="b1"\nversion="0.4.12"\nfallback_branch="minds-v0.4.12"\nrollout_percentage=100\n'
    )
    assert [entry.channel for entry in entries] == ["stable"]


def test_a_channel_nobody_serves_is_still_refused() -> None:
    with pytest.raises(PromotionError, match="Unknown channel"):
        parse_channels(
            '[channels.nightly]\nbuild_id="b"\nversion="1.0.0"\nfallback_branch="t"\nrollout_percentage=100\n'
        )


def test_a_channel_missing_a_field_is_rejected_before_any_network_call() -> None:
    with pytest.raises(PromotionError, match="fallback_branch field required"):
        parse_channels('[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\nrollout_percentage=100\n')


@pytest.mark.parametrize("declared", ["false", "0", "1.0", "[]", '""'])
def test_a_string_field_that_is_not_a_string_is_refused_by_name(declared: str) -> None:
    """`build_id` reaches a URL, so a malformed one must not become a 404 from a fetch."""
    with pytest.raises(
        PromotionError, match="build_id (input should be a valid string|string should have at least 1 character)"
    ):
        parse_channels(
            f"[channels.alpha]\nbuild_id={declared}\n"
            'version="0.4.12"\nfallback_branch="minds-v0.4.12"\nrollout_percentage=100\n'
        )


def test_a_padded_string_field_is_stripped() -> None:
    """`build_id` reaches a URL, so the value that is kept has to be the one that was checked."""
    (entry,) = parse_channels(
        '[channels.alpha]\nbuild_id=" b1 "\nversion="\t0.4.12"\n'
        'fallback_branch="minds-v0.4.12 "\nrollout_percentage=100\n'
    )
    assert (entry.build_id, entry.version, entry.fallback_branch) == ("b1", "0.4.12", "minds-v0.4.12")


def test_an_entry_that_is_not_a_table_is_refused_by_name() -> None:
    """Writing the build id straight under [channels] is the ordinary typo here.

    It has to refuse the same way every other malformed file does. `main` catches
    only PromotionError, so anything else is a traceback in the promote job's
    output for a mistake in a hand-edited file.
    """
    with pytest.raises(PromotionError, match=r"\[channels.alpha\] must be a table"):
        parse_channels('[channels]\nalpha = "260813on4zui9xf"\n')
    with pytest.raises(PromotionError, match="`channels` must be a table"):
        parse_channels('channels = "alpha"\n')


def test_a_misspelled_table_is_refused_rather_than_dropped() -> None:
    """The typo that skips one channel while publishing the others, green.

    A misspelled field is already refused by name; a misspelled table used to be
    discarded, so `[chanels.beta]` beside a good `[channels.stable]` published
    stable, left beta on its old build, and exited zero. The shipped-file test
    cannot see it either: entries is non-empty, so the suite stays green.
    """
    good = (
        '[channels.stable]\nbuild_id="b1"\nversion="0.4.12"\nfallback_branch="minds-v0.4.12"\nrollout_percentage=100\n'
    )
    with pytest.raises(PromotionError, match=r"Unknown top-level key\(s\) \['chanels'\]"):
        parse_channels(good + '[chanels.beta]\nbuild_id="b2"\nversion="0.4.12"\n')


def test_a_table_that_also_declares_channel_is_refused_by_name() -> None:
    """The one key `extra="forbid"` cannot refuse, because it is a real field.

    It reaches the constructor twice and raises a TypeError, which `main` does
    not catch -- so without this the promote job gets a traceback where every
    other malformed key gets the key by name.
    """
    with pytest.raises(PromotionError, match=r"\[channels.alpha\] declares `channel`"):
        parse_channels(
            '[channels.alpha]\nchannel="beta"\nbuild_id="b1"\n'
            'version="0.4.12"\nfallback_branch="minds-v0.4.12"\nrollout_percentage=100\n'
        )


def test_an_empty_file_declares_nothing() -> None:
    assert parse_channels("") == ()


def test_a_version_that_disagrees_with_the_build_is_rejected() -> None:
    """The file is what the reviewer reads, so it must say what it does."""
    with pytest.raises(PromotionError, match="says version 0.9.9, but build b1 is version 0.4.12"):
        assert_version_matches_build(
            ChannelEntry(channel="alpha", build_id="b1", version="0.9.9", fallback_branch="t", rollout_percentage=100),
            "0.4.12",
        )


def test_a_dry_run_reports_the_move_without_publishing() -> None:
    assert "would publish 0.4.12" in _apply(ALPHA, served=_some_other_build_at("0.4.10"))


def test_a_channel_already_at_the_declared_build_is_a_no_op() -> None:
    """Re-running the promotion that is already applied must not republish."""
    assert "already serving build b1" in _apply(ALPHA, served=PUBLISHED_B1)


def test_a_new_build_at_the_same_version_is_still_published() -> None:
    """The ordinary alpha promotion: versions are stamped at cut, so every build
    between two cuts repeats the last cut's version.

    Comparing versions instead of manifests would make this report success while
    leaving the channel on the previous build.
    """
    assert "would publish 0.4.12" in _apply(ALPHA, served=_some_other_build_at("0.4.12"))


def test_a_real_run_uploads_the_rewritten_manifest_under_the_channel_key(stub_s3_client: Any) -> None:
    """Every other case here dry-runs, so this is the only one that writes.

    upload_manifest is proven on its own elsewhere; what is proven here is that
    apply_entry reaches it with this entry's bucket, channel and manifest.
    """
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "bucket",
                "Key": "alpha-mac.yml",
                "Body": PUBLISHED_B1.encode("utf-8"),
                "ContentType": "text/yaml",
                "CacheControl": "public, max-age=60",
            },
        )
        result = _apply(ALPHA, served=_some_other_build_at("0.4.10"), dry_run=False, make_client=lambda: client)
        stubber.assert_no_pending_responses()
    assert "published 0.4.12 to 100% (was 0.4.10 to 100% (declaring no rollout))" in result


def test_a_never_published_channel_is_a_first_publish() -> None:
    assert "currently nothing" in _apply(ALPHA, served=None)


def test_editing_the_file_backwards_publishes_and_says_so() -> None:
    """A withdrawal goes through the reviewed file like any other move.

    Nobody on the newer version is pulled back, so what changes is what a new
    download gets -- which is why the line names the connector fallback that has
    to move with it.
    """
    report = _apply(ALPHA, served=_some_other_build_at("0.4.20"))
    assert "BACKWARDS" in report
    assert "connector download fallback" in report


def test_a_prerelease_build_version_is_refused_before_the_upload() -> None:
    """The stamp-once rule reaches a run only through the BACKWARDS label.

    `_version_key` is where a plain X.Y.Z is required, and its one caller is the
    ternary deciding whether the line ends in BACKWARDS -- so a label computed
    only for a channel that has been published before would drop the rule for
    the promotion it matters most for, which is turning a channel on.
    """
    prerelease = TODESKTOP_MANIFEST.replace("0.4.12", "0.5.0-alpha.1")
    entry = ChannelEntry(
        channel="alpha",
        build_id="b1",
        version="0.5.0-alpha.1",
        fallback_branch="minds-v0.5.0-alpha.1",
        rollout_percentage=100,
    )
    with pytest.raises(PromotionError, match="plain X.Y.Z"):
        _apply(entry, served=None, lima_image_base_url=None, fetch=_fetch(served=None, build=prerelease))


def test_a_fallback_branch_left_on_the_previous_release_is_rejected() -> None:
    """The likeliest bad edit: bump build_id and version, leave the tag beside them.

    The image gate would then find the PREVIOUS release's image and pass, while
    clients ask for the tag the binary ships, 404, and silently build in-VM.
    """
    stale = ChannelEntry(
        channel="alpha", build_id="b1", version="0.4.12", fallback_branch="minds-v0.4.11", rollout_percentage=100
    )
    with pytest.raises(PromotionError, match="says fallback_branch minds-v0.4.11.*clones minds-v0.4.12"):
        _apply(stale, served=None)


def test_a_channel_the_file_no_longer_declares_is_reported_as_still_serving() -> None:
    """Removing an entry publishes nothing, so the channel keeps its last build.

    That makes the one edit a reader reaches for to withdraw a bad promotion --
    `git revert` on the commit that added the entry -- a run that reports success
    having changed nothing. It has to be said out loud.
    """
    reports = undeclared_channel_reports((), **_feed_reads(PUBLISHED_B1))
    assert len(reports) == len(PUBLISHABLE_CHANNELS)
    assert all("still serving 0.4.12" in report for report in reports)
    assert any(report.startswith("alpha:") for report in reports)


def test_a_channel_nobody_has_ever_published_to_is_not_reported() -> None:
    """Beta today. An undeclared channel with no manifest is the ordinary state."""
    assert undeclared_channel_reports((), **_feed_reads(None)) == ()


def test_a_declared_channel_is_never_reported_as_undeclared() -> None:
    reports = undeclared_channel_reports((ALPHA,), **_feed_reads(PUBLISHED_B1))
    assert not any(report.startswith("alpha:") for report in reports)


def test_a_refused_promotion_exits_non_zero(tmp_path: Path) -> None:
    """The promote job's red is this exit code, and nothing else asserts it.

    Every gate in here is only worth its refusal reaching the operator, and the
    only thing that carries it out of the process is `main` turning a
    PromotionError into a ClickException rather than echoing it.
    """
    channels_file = tmp_path / "release-channels.toml"
    channels_file.write_text(
        '[channels.nightly]\nbuild_id="b"\nversion="1.0.0"\nfallback_branch="minds-v1.0.0"\nrollout_percentage=100\n'
    )
    result = CliRunner().invoke(
        main,
        [
            "--channels-file",
            str(channels_file),
            "--app-id",
            APP_ID,
            "--bucket",
            "bucket",
            "--feed-base-url",
            FEED,
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert "Unknown channel(s) ['nightly']" in result.output


def test_a_tier_with_no_image_store_publishes_without_the_image_gate() -> None:
    """Production's configuration, and the only path CI actually takes.

    Every other case here supplies an image store, so without this the one branch
    that runs in production is the one branch nothing exercises -- which is how
    the gate's arch comparison could be wrong for as long as it was.
    """

    def refusing_image_fetch(url: str) -> bytes:
        assert "/manifests/" not in url, "the image gate ran on a tier that configures none"
        return _fetch(served=None)(url)

    report = _apply(ALPHA, served=None, lima_image_base_url=None, fetch=refusing_image_fetch)

    assert report == "alpha: would publish 0.4.12 to 100% (currently nothing)"


def test_the_version_check_runs_before_the_lima_gate() -> None:
    """A mismatched file should fail on the thing the reviewer can see, not a remote lookup."""
    wrong = ChannelEntry(
        channel="alpha", build_id="b1", version="0.9.9", fallback_branch="minds-v0.4.12", rollout_percentage=100
    )
    with pytest.raises(PromotionError, match="says version 0.9.9"):
        _apply(wrong, served=None)


def test_a_credentialed_run_reads_the_bucket_through_the_client_it_was_handed(stub_s3_client: Any) -> None:
    """`make_client` has to reach the read of what a channel serves, not only the upload.

    The credentialed run is the only one that reads the bucket, so a client that
    stops at `upload_manifest` leaves that read building its own from the
    environment -- which is correct only for as long as nothing injects one, and
    leaves the branch production takes with no way to be tested.
    """

    def refusing_feed_fetch(url: str) -> bytes:
        assert not url.endswith("-mac.yml"), "the feed was read on a run that says it reads the bucket"
        return _fetch(served=None)(url)

    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": BytesIO(b"version: 0.4.10\n")},
            expected_params={"Bucket": "bucket", "Key": "alpha-mac.yml"},
        )
        report = _apply(ALPHA, served=None, from_bucket=True, fetch=refusing_feed_fetch, make_client=lambda: client)
        stubber.assert_no_pending_responses()
    assert report == "alpha: would publish 0.4.12 to 100% (currently 0.4.10 to 100% (declaring no rollout))"


def test_the_current_state_is_read_from_the_bucket_when_asked_for_it(stub_s3_client: Any) -> None:
    """What a channel serves, taken from the object rather than its cached copy.

    Proven by giving the feed a body the bucket does not have: a run that says
    bucket and reads the feed would return the feed's version.
    """
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": BytesIO(b"version: 0.4.20\n")},
            expected_params={"Bucket": "bucket", "Key": "alpha-mac.yml"},
        )
        current = read_current_channel_manifest(
            "alpha",
            bucket="bucket",
            feed_base_url=FEED,
            from_bucket=True,
            fetch=_fetch(served=_some_other_build_at("0.4.10")),
            make_client=lambda: client,
        )
        stubber.assert_no_pending_responses()
    assert current is not None and version_of(current) == "0.4.20"


def test_the_current_state_falls_back_to_the_feed_without_a_credential() -> None:
    """The validate job holds none by design, and publishes nothing.

    It is the one run that can afford a stale answer: the worst a cached read
    costs there is a preview that disagrees with the publish that follows.
    """
    current = read_current_channel_manifest(
        "alpha",
        bucket="bucket",
        feed_base_url=FEED,
        from_bucket=False,
        fetch=_fetch(served=_some_other_build_at("0.4.10")),
        make_client=_no_client,
    )
    assert current is not None and version_of(current) == "0.4.10"


def test_a_misspelled_field_is_named_beside_the_one_it_should_have_been() -> None:
    """A misspelled percentage is the dangerous typo: absence rolls out to everyone.

    Both halves of the difference are reported, so the refusal shows what was
    written next to what was meant rather than leaving the two to be compared.
    """
    with pytest.raises(
        PromotionError,
        match="rollout_percentage field required; rollout_percentge extra inputs are not permitted",
    ):
        parse_channels(
            '[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\n'
            'fallback_branch="minds-v0.4.12"\nrollout_percentge=10\n'
        )


def test_a_missing_rollout_is_refused_because_absence_means_everyone() -> None:
    with pytest.raises(PromotionError, match="rollout_percentage field required"):
        parse_channels('[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\nfallback_branch="minds-v0.4.12"\n')


def test_a_rollout_of_zero_is_a_value_and_not_an_absence() -> None:
    """0 means nobody and absence means everyone, so reading one as the other inverts it.

    The usual `if not fields.get(f)` idiom would silently turn this into the
    largest possible rollout.
    """
    entries = parse_channels(
        '[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\nfallback_branch="minds-v0.4.12"\nrollout_percentage=0\n'
    )
    assert entries[0].rollout_percentage == 0
    assert read_rollout_percentage(parse_manifest(published_b1_at(0), "x"), "x") == 0


@pytest.mark.parametrize("declared", ['"10"', "10.5", "true"])
def test_a_rollout_that_is_not_a_whole_number_is_refused(declared: str) -> None:
    """electron-updater includes every install for a non-numeric value and truncates a float."""
    with pytest.raises(PromotionError, match="rollout_percentage input should be a valid integer"):
        parse_channels(
            '[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\n'
            f'fallback_branch="minds-v0.4.12"\nrollout_percentage={declared}\n'
        )


@pytest.mark.parametrize("declared", ["150", "-5"])
def test_a_rollout_outside_the_range_is_refused(declared: str) -> None:
    """Nothing clamps downstream: above 100 reaches everyone, below 0 reaches nobody."""
    with pytest.raises(PromotionError, match="rollout_percentage input should be (less|greater) than or equal to"):
        parse_channels(
            '[channels.alpha]\nbuild_id="b1"\nversion="0.4.12"\n'
            f'fallback_branch="minds-v0.4.12"\nrollout_percentage={declared}\n'
        )


def test_every_report_names_the_rollout() -> None:
    """A ramp step changes nothing else, and the dry run is all a reviewer sees."""
    assert "to 10%" in _apply(alpha_at(10), served=None)
    assert "to 30%" in _apply(alpha_at(30), served=published_b1_at(10))
    assert "to 10%" in _apply(alpha_at(10), served=published_b1_at(10))


def test_a_ramp_step_republishes_the_same_build() -> None:
    """The step changes only the percentage, so a version comparison would skip it."""
    assert "would publish 0.4.12 to 30%" in _apply(alpha_at(30), served=published_b1_at(10))


def test_re_running_a_ramp_step_publishes_nothing() -> None:
    assert "nothing to do" in _apply(alpha_at(10), served=published_b1_at(10))


def test_narrowing_a_ramp_publishes_and_stops_the_bleed() -> None:
    """Lowering the percentage is how a bad build is stopped part-way through a ramp.

    electron-updater re-reads the percentage on every check and buckets each
    install off a fixed id, so a narrower band is a strictly smaller one: whoever
    has not polled yet stops being offered the build. It recalls nobody who
    already took it, which is why this is a partial halt rather than a rollback.
    """
    assert "would publish 0.4.12 to 10%" in _apply(alpha_at(10), served=published_b1_at(50))


def test_the_uploaded_manifest_carries_the_rollout(stub_s3_client: Any) -> None:
    """What the channel actually serves, rather than what the entry declared."""
    client = stub_s3_client
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            expected_params={
                "Bucket": "bucket",
                "Key": "alpha-mac.yml",
                "Body": published_b1_at(10).encode("utf-8"),
                "ContentType": "text/yaml",
                "CacheControl": "public, max-age=60",
            },
        )
        _apply(alpha_at(10), served=None, dry_run=False, make_client=lambda: client)
        stubber.assert_no_pending_responses()


def test_a_served_rollout_the_reader_refuses_stops_the_promotion_before_the_upload() -> None:
    """A refusal reached after the upload would leave the channel already moved.

    Only the line naming what the channel serves today reads the served
    percentage, and it is built before anything is written. `_no_client` is what
    proves nothing was written.
    """
    served = _some_other_build_at("0.4.10") + "stagingPercentage: ten\n"
    with pytest.raises(PromotionError, match="alpha-mac.yml declares stagingPercentage 'ten'"):
        _apply(ALPHA, served=served, dry_run=False, make_client=_no_client)
