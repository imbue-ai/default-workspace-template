import time

import pytest

from workspace_update_control.channel import (
    CHANNEL_READ_INTERVAL_SECONDS,
    CLIENT_CEILING_HOLD_SECONDS,
    ChannelFileUnreadableError,
    bounded_by_client_ceiling,
    channel_file_url,
    fetch_channel_file,
    is_channel_version_current,
    parse_channel_file,
    read_channel_version,
    read_fresh_client_ceiling,
    record_client_ceiling,
    refresh_channel_version,
)
from workspace_update_control.settings import UpdateSettings
from workspace_update_control.testing import (
    DRIPPING_FEED_CHANNEL,
    OVERSIZED_FEED_CHANNEL,
    SILENT_FEED_CHANNEL,
    STAGED_FEED_BASE_URL,
    RecordingFeed,
    StagedUpdateWorkspace,
    update_settings_document,
)

_NOW = 1_792_000_000.0
_STABLE_FILE_URL = f"{STAGED_FEED_BASE_URL}/stable-web.json"


def _settings(**fields: object) -> UpdateSettings:
    return UpdateSettings(**update_settings_document(**fields))


def test_a_channels_file_is_beside_the_feeds_other_files_under_the_channels_name() -> (
    None
):
    assert channel_file_url("https://feed.example/updates/", "beta") == (
        "https://feed.example/updates/beta-web.json"
    )


def test_the_channels_file_names_the_release_under_template_ref() -> None:
    assert parse_channel_file('{"templateRef": " minds-v0.10.5 "}') == "minds-v0.10.5"


@pytest.mark.parametrize(
    "text",
    [
        "<html>not found</html>",
        "[]",
        "{}",
        '{"templateRef": 7}',
        # A channel is promoted to a release, never to a branch or a prerelease.
        '{"templateRef": "main"}',
        '{"templateRef": "minds-v0.10.5-rc.1"}',
    ],
)
def test_a_file_that_names_no_release_is_unreadable(text: str) -> None:
    with pytest.raises(ChannelFileUnreadableError):
        parse_channel_file(text)


def test_the_first_read_asks_the_feed_and_keeps_the_answer(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed("minds-v0.10.5")

    state = refresh_channel_version(_settings(), _NOW, feed, is_due_when_unread=False)

    assert feed.asked == [_STABLE_FILE_URL]
    assert state == {
        "schema": 1,
        "channel": "stable",
        "feed_base_url": STAGED_FEED_BASE_URL,
        "template_ref": "minds-v0.10.5",
        "read_at": _NOW,
        "checked_at": _NOW,
        "error": "",
    }
    assert staged.read_state("channel-version.json") == state
    assert read_channel_version(_settings()) == state
    assert is_channel_version_current(state) is True


def test_the_feed_is_asked_at_most_once_an_hour(staged: StagedUpdateWorkspace) -> None:
    feed = RecordingFeed("minds-v0.10.5")
    refresh_channel_version(_settings(), _NOW, feed, is_due_when_unread=False)
    feed.template_ref = "minds-v0.11.0"

    for minute in range(1, 60):
        within_the_hour = refresh_channel_version(
            _settings(), _NOW + minute * 60, feed, is_due_when_unread=False
        )
        assert within_the_hour is not None
        assert within_the_hour["template_ref"] == "minds-v0.10.5"
    assert len(feed.asked) == 1

    after_the_hour = refresh_channel_version(
        _settings(),
        _NOW + CHANNEL_READ_INTERVAL_SECONDS,
        feed,
        is_due_when_unread=False,
    )

    assert len(feed.asked) == 2
    assert after_the_hour is not None
    assert (after_the_hour["template_ref"], after_the_hour["read_at"]) == (
        "minds-v0.11.0",
        _NOW + CHANNEL_READ_INTERVAL_SECONDS,
    )


def test_a_read_that_fails_keeps_the_last_version_and_says_why_it_failed(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed("minds-v0.10.5")
    refresh_channel_version(_settings(), _NOW, feed, is_due_when_unread=False)
    feed.template_ref = None
    later = _NOW + CHANNEL_READ_INTERVAL_SECONDS

    state = refresh_channel_version(_settings(), later, feed, is_due_when_unread=False)

    assert state is not None
    assert (state["template_ref"], state["read_at"], state["checked_at"]) == (
        "minds-v0.10.5",
        _NOW,
        later,
    )
    assert state["error"] == "the feed is down"
    assert is_channel_version_current(state) is False


def test_a_feed_that_was_never_read_is_not_asked_again_within_the_hour_by_the_dispatcher(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed(None)

    first = refresh_channel_version(_settings(), _NOW, feed, is_due_when_unread=False)
    refresh_channel_version(_settings(), _NOW + 60, feed, is_due_when_unread=False)

    assert first is not None
    assert (first["template_ref"], first["read_at"], first["error"]) == (
        "",
        None,
        "the feed is down",
    )
    assert len(feed.asked) == 1


def test_a_request_that_needs_a_version_asks_again_while_none_was_ever_read(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed(None)
    refresh_channel_version(_settings(), _NOW, feed, is_due_when_unread=False)
    feed.template_ref = "minds-v0.10.5"

    state = refresh_channel_version(
        _settings(), _NOW + 60, feed, is_due_when_unread=True
    )

    assert len(feed.asked) == 2
    assert state is not None
    assert state["template_ref"] == "minds-v0.10.5"
    # Once a version is read, such a request waits out the hour like the dispatcher.
    refresh_channel_version(_settings(), _NOW + 120, feed, is_due_when_unread=True)
    assert len(feed.asked) == 2


def test_a_version_read_for_another_channel_is_not_this_channels(
    staged: StagedUpdateWorkspace,
) -> None:
    refresh_channel_version(
        _settings(), _NOW, RecordingFeed("minds-v0.10.5"), is_due_when_unread=False
    )
    beta = _settings(channel="beta")
    feed = RecordingFeed(None)

    assert read_channel_version(beta) is None

    state = refresh_channel_version(beta, _NOW + 60, feed, is_due_when_unread=False)

    assert feed.asked == [f"{STAGED_FEED_BASE_URL}/beta-web.json"]
    assert state is not None
    assert (state["channel"], state["template_ref"]) == ("beta", "")


def test_settings_that_name_no_feed_read_no_version(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed("minds-v0.10.5")

    state = refresh_channel_version(
        _settings(feed_base_url=""), _NOW, feed, is_due_when_unread=True
    )

    assert state is None
    assert feed.asked == []
    assert staged.read_state("channel-version.json") is None


def test_a_clients_newest_version_stands_for_a_few_of_its_sweeps_and_then_lapses(
    staged: StagedUpdateWorkspace,
) -> None:
    assert read_fresh_client_ceiling(_NOW) is None

    record_client_ceiling("minds-v0.10.5", _NOW)

    assert staged.read_state("client-ceiling.json") == {
        "schema": 1,
        "ceiling": "minds-v0.10.5",
        "asserted_at": _NOW,
    }
    assert staged.read_state("schedule.json") is None
    assert (
        read_fresh_client_ceiling(_NOW + CLIENT_CEILING_HOLD_SECONDS - 1)
        == "minds-v0.10.5"
    )
    assert read_fresh_client_ceiling(_NOW + CLIENT_CEILING_HOLD_SECONDS) is None
    # Three sweeps of a desktop that sweeps every 300 seconds.
    assert CLIENT_CEILING_HOLD_SECONDS == 3 * 300


@pytest.mark.parametrize(
    ("channel_ref", "client_ceiling", "expected"),
    [
        ("minds-v0.11.0", None, "minds-v0.11.0"),
        ("minds-v0.11.0", "minds-v0.10.5", "minds-v0.10.5"),
        ("minds-v0.10.5", "minds-v0.11.0", "minds-v0.10.5"),
        ("minds-v0.11.0", "minds-v0.11.0", "minds-v0.11.0"),
        # A development build names its branch, which says nothing of which is older.
        ("minds-v0.11.0", "main", None),
    ],
)
def test_the_channels_version_is_held_to_the_version_a_client_still_asking_named(
    channel_ref: str, client_ceiling: str | None, expected: str | None
) -> None:
    assert bounded_by_client_ceiling(channel_ref, client_ceiling) == expected


def test_the_channels_file_is_read_over_http(local_feed: str) -> None:
    text = fetch_channel_file(channel_file_url(local_feed, "stable"))

    assert parse_channel_file(text) == "minds-v0.10.5"


def test_a_channel_the_feed_does_not_publish_is_unreadable(local_feed: str) -> None:
    with pytest.raises(ChannelFileUnreadableError, match="404"):
        fetch_channel_file(channel_file_url(local_feed, "nightly"))


@pytest.mark.parametrize(
    ("channel", "why"),
    [
        # The socket's own wait and the read's whole time run out together, so either may say it.
        (SILENT_FEED_CHANNEL, "did not answer|timed out"),
        # No single wait on the socket runs out, so only giving the read up ends it.
        (DRIPPING_FEED_CHANNEL, "did not answer in 0.5 seconds"),
    ],
)
def test_a_feed_that_keeps_the_read_waiting_is_given_up_on_when_the_time_is_out(
    local_feed: str, channel: str, why: str
) -> None:
    started_at = time.monotonic()

    with pytest.raises(ChannelFileUnreadableError, match=why):
        fetch_channel_file(channel_file_url(local_feed, channel), timeout_seconds=0.5)

    assert time.monotonic() - started_at < 5


def test_a_file_larger_than_any_channels_file_is_unreadable(local_feed: str) -> None:
    with pytest.raises(ChannelFileUnreadableError, match="larger than"):
        fetch_channel_file(channel_file_url(local_feed, OVERSIZED_FEED_CHANNEL))


def test_an_address_no_request_can_be_made_for_is_unreadable_and_says_why() -> None:
    started_at = time.monotonic()

    with pytest.raises(ChannelFileUnreadableError, match="Invalid IPv6 URL"):
        fetch_channel_file("https://[feed.example/stable-web.json")

    assert time.monotonic() - started_at < 5


def test_an_address_that_is_not_http_is_never_opened(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    secret = tmp_path_factory.mktemp("feed") / "stable-web.json"
    secret.write_text('{"templateRef": "minds-v0.10.5"}')

    with pytest.raises(ChannelFileUnreadableError, match="not an http address"):
        fetch_channel_file(secret.as_uri())
