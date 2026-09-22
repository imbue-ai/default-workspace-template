import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from imbue.minds_evals.cli import main
from imbue.minds_evals.cli import run_post_slack_report
from imbue.minds_evals.errors import SlackPostError
from imbue.minds_evals.mock_clock_test import ManualClock
from imbue.minds_evals.mock_slack_poster_test import MockSlackPoster
from imbue.minds_evals.slack_post import DEFAULT_RATE_LIMIT_WAIT_SECONDS
from imbue.minds_evals.slack_post import MAX_RATE_LIMITED_ATTEMPT_COUNT
from imbue.minds_evals.slack_post import MAX_RATE_LIMIT_WAIT_SECONDS
from imbue.minds_evals.slack_post import MAX_SLACK_TEXT_CHARACTERS
from imbue.minds_evals.slack_post import NO_MESSAGE_TS
from imbue.minds_evals.slack_post import RETRY_AFTER_HEADER
from imbue.minds_evals.slack_post import SLACK_BOT_TOKEN_ENV_VAR
from imbue.minds_evals.slack_post import SLACK_POST_MESSAGE_URL
from imbue.minds_evals.slack_post import SLACK_POST_TIMEOUT_SECONDS
from imbue.minds_evals.slack_post import SLACK_RATE_LIMITED_ERROR
from imbue.minds_evals.slack_post import SLACK_WEBHOOK_ENV_VAR
from imbue.minds_evals.slack_post import SlackApiPoster
from imbue.minds_evals.slack_post import SlackPostOutcome
from imbue.minds_evals.slack_post import SlackThreadPayload
from imbue.minds_evals.slack_post import TEXT_CUT_NOTICE
from imbue.minds_evals.slack_post import WebhookPoster
from imbue.minds_evals.slack_post import check_webhook_answer
from imbue.minds_evals.slack_post import clamp_payload_text
from imbue.minds_evals.slack_post import post_threads
from imbue.minds_evals.slack_post import poster_from_environment
from imbue.minds_evals.slack_post import read_api_answer
from imbue.minds_evals.slack_post import without_blocks

CHANNEL: str = "C0BUFUVU0T0"

WEBHOOK_URL: str = "https://hooks.slack.test/services/T0BUFUVU0T0/B0BUFUVU0T0/rP3Kj9sQ2vXn"


def post_report(
    poster: MockSlackPoster, threads: Sequence[SlackThreadPayload], clock: ManualClock | None = None
) -> SlackPostOutcome:
    """Post a report against a stand-in Slack, on a clock whose waits cost no real time.

    A clock is passed in by the tests that read the waits back off it; the rest get one of their own,
    since even a pass that waits nothing has to be handed something to wait on.
    """
    return asyncio.run(post_threads(poster, clock if clock is not None else ManualClock(), CHANNEL, threads))


def make_payload(text: str) -> dict[str, Any]:
    """One message as `ci-report` writes it: the report as text, and the blocks that draw it."""
    return {
        "username": "Evals",
        "icon_emoji": ":big_brain:",
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def make_thread(text: str, reply_texts: tuple[str, ...]) -> SlackThreadPayload:
    return SlackThreadPayload(message=make_payload(text), replies=tuple(make_payload(reply) for reply in reply_texts))


def write_payloads(payloads_path: Path, threads: tuple[SlackThreadPayload, ...]) -> Path:
    payloads_path.write_text(json.dumps([thread.model_dump(mode="json") for thread in threads]))
    return payloads_path


def test_post_threads_posts_each_reply_under_the_message_it_belongs_to() -> None:
    """A reply is threaded under its own message's `ts`, which is the whole reason the run posts with
    a bot token: posted flat, a table of scores is a report about nothing."""
    poster = MockSlackPoster()

    outcome = post_report(poster, [make_thread("first arm", ("scores", "diagnostics")), make_thread("second arm", ())])

    assert [post.payload["text"] for post in poster.posts] == [
        "first arm",
        "scores",
        "diagnostics",
        "second arm",
    ]
    first_ts = poster.posts[0].thread_ts
    assert first_ts == NO_MESSAGE_TS
    assert [post.thread_ts for post in poster.posts[1:3]] == ["17000000.000000", "17000000.000000"]
    assert poster.posts[3].thread_ts == NO_MESSAGE_TS
    assert [post.channel for post in poster.posts] == [CHANNEL] * 4
    # Compared whole, so that a pass in which nothing went wrong is pinned as saying nothing went
    # wrong -- a count that drifted would otherwise read as a report the channel is missing part of.
    assert outcome == SlackPostOutcome(posted_count=4, fallback_count=0, failed_count=0, warnings=())


def test_post_threads_reposts_a_message_as_text_when_slack_refuses_its_blocks() -> None:
    """A refusal is most likely a block type the workspace will not render, which the report cannot
    know about in advance. The message's own text says everything its blocks do, so it is posted
    again without them -- and the reply still threads under whatever that retry landed as."""
    poster = MockSlackPoster(blocks_rejecting_call_indexes=frozenset({0}))

    outcome = post_report(poster, [make_thread("first arm", ("scores",))])

    assert [("blocks" in post.payload, post.thread_ts) for post in poster.posts] == [
        (True, NO_MESSAGE_TS),
        (False, NO_MESSAGE_TS),
        (True, "17000000.000001"),
    ]
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (2, 1, 0)
    assert outcome.warnings == ("Slack rejected the blocks of report message 1; posting its text instead",)


def test_post_threads_reposts_a_refused_reply_as_text_too() -> None:
    """The replies carry tables, which are the blocks a workspace is most likely to refuse, so the
    same retry has to cover them -- otherwise the scores behind a grid are the part that goes
    missing."""
    poster = MockSlackPoster(blocks_rejecting_call_indexes=frozenset({1}))

    outcome = post_report(poster, [make_thread("first arm", ("scores",))])

    assert [("blocks" in post.payload, post.payload["text"]) for post in poster.posts] == [
        (True, "first arm"),
        (True, "scores"),
        (False, "scores"),
    ]
    assert poster.posts[2].thread_ts == poster.posts[1].thread_ts
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (2, 1, 0)


def test_post_threads_skips_the_replies_of_a_message_that_cannot_be_threaded() -> None:
    """A webhook answers with no message id, so nothing can be posted under what it posted. The
    replies are skipped and said out loud rather than posted beside the message, where a table of
    scores with no message over it reads as a report about nothing."""
    poster = MockSlackPoster(is_threading_supported=False)

    outcome = post_report(poster, [make_thread("first arm", ("scores", "diagnostics"))])

    assert [post.payload["text"] for post in poster.posts] == ["first arm"]
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (1, 0, 0)
    assert outcome.warnings == ("report message 1 could not be threaded, so its 2 replies were not posted",)


def test_post_threads_still_posts_the_next_thread_after_one_that_slack_refused() -> None:
    """Each thread is the whole report of one arm, so losing the rest of a night to the first refusal
    is exactly what this must not do. A message Slack would not take at all keeps its replies out of
    the channel, since there is nothing for them to hang under."""
    poster = MockSlackPoster(failing_call_indexes=frozenset({0}))

    outcome = post_report(poster, [make_thread("first arm", ("scores",)), make_thread("second arm", ())])

    assert [post.payload["text"] for post in poster.posts] == ["first arm", "second arm"]
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (1, 0, 1)
    # The refusal is named once: a message Slack would not take is not also reported as one that
    # could not be threaded.
    assert outcome.warnings == ("Slack would not take report message 1: Slack refused the message: channel_not_found",)


def test_post_threads_reports_a_message_whose_text_slack_would_not_take_either() -> None:
    """The fallback is the last thing that can be done for a message, so a refusal of it is the end
    of that message -- and the reader is told twice: what was tried, and that it did not work."""
    poster = MockSlackPoster(blocks_rejecting_call_indexes=frozenset({0}), failing_call_indexes=frozenset({1}))

    outcome = post_report(poster, [make_thread("first arm", ("scores",))])

    assert len(poster.posts) == 2
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (0, 0, 1)
    assert outcome.warnings == (
        "Slack rejected the blocks of report message 1; posting its text instead",
        "Slack would not take the text of report message 1 either: Slack refused the message: channel_not_found",
    )


def test_post_threads_waits_the_retry_after_slack_named_and_posts_the_message_again() -> None:
    """A thread's messages go out back to back, faster than the message a second per channel Slack
    allows, so a burst of them is refused rather than queued. Waiting the `Retry-After` out is all
    such a refusal needs -- and a top message dropped here would take its replies with it."""
    clock = ManualClock()
    poster = MockSlackPoster(rate_limited_answer_count=1, rate_limit_retry_after_seconds=7.0)
    started_at = clock.now()

    outcome = post_report(poster, [make_thread("first arm", ("scores",))], clock)

    assert clock.now() - started_at == 7.0
    assert [post.payload["text"] for post in poster.posts] == ["first arm", "first arm", "scores"]
    assert poster.posts[2].thread_ts == "17000000.000001"
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (2, 0, 0)
    # A refusal that a wait settled is not a `::warning::` line: nothing about the report is missing.
    assert outcome.warnings == ()


def test_post_threads_waits_the_default_when_slack_named_no_retry_after() -> None:
    """Slack does not always send the header, and a rate limit with no figure on it still has to be
    waited out rather than reported."""
    clock = ManualClock()
    poster = MockSlackPoster(rate_limited_answer_count=1)
    started_at = clock.now()

    outcome = post_report(poster, [make_thread("first arm", ())], clock)

    assert clock.now() - started_at == DEFAULT_RATE_LIMIT_WAIT_SECONDS
    assert (outcome.posted_count, outcome.failed_count) == (1, 0)


def test_post_threads_waits_the_default_when_slack_asked_for_no_wait_at_all() -> None:
    """A `Retry-After` of zero would post the message straight back into the rate limit that refused
    it, spending an attempt on a refusal nothing had time to clear."""
    clock = ManualClock()
    poster = MockSlackPoster(rate_limited_answer_count=1, rate_limit_retry_after_seconds=0.0)
    started_at = clock.now()

    outcome = post_report(poster, [make_thread("first arm", ())], clock)

    assert clock.now() - started_at == DEFAULT_RATE_LIMIT_WAIT_SECONDS
    assert (outcome.posted_count, outcome.failed_count) == (1, 0)


def test_post_threads_caps_the_wait_a_retry_after_can_ask_for() -> None:
    """A `Retry-After` of minutes means the channel is throttling far more than this report, and
    holding the run's job open for it costs more than the message being waited on."""
    clock = ManualClock()
    poster = MockSlackPoster(rate_limited_answer_count=1, rate_limit_retry_after_seconds=3600.0)
    started_at = clock.now()

    outcome = post_report(poster, [make_thread("first arm", ())], clock)

    assert clock.now() - started_at == MAX_RATE_LIMIT_WAIT_SECONDS
    assert (outcome.posted_count, outcome.failed_count) == (1, 0)


def test_post_threads_gives_up_on_a_rate_limit_that_outlasts_the_attempt_bound() -> None:
    """Past the bound the channel is under load from somewhere else, which waiting longer does not
    fix, so the refusal is reported the way every other one is: a warning naming it, the replies kept
    out of the channel, and the next thread posted all the same."""
    clock = ManualClock()
    poster = MockSlackPoster(rate_limited_answer_count=MAX_RATE_LIMITED_ATTEMPT_COUNT)
    started_at = clock.now()

    outcome = post_report(poster, [make_thread("first arm", ("scores",)), make_thread("second arm", ())], clock)

    assert [post.payload["text"] for post in poster.posts] == ["first arm"] * MAX_RATE_LIMITED_ATTEMPT_COUNT + [
        "second arm"
    ]
    assert clock.now() - started_at == (MAX_RATE_LIMITED_ATTEMPT_COUNT - 1) * DEFAULT_RATE_LIMIT_WAIT_SECONDS
    assert (outcome.posted_count, outcome.fallback_count, outcome.failed_count) == (1, 0, 1)
    assert outcome.warnings == ("Slack would not take report message 1: Slack rate-limited the message",)


def test_without_blocks_keeps_everything_the_message_says() -> None:
    """The fallback is the same message without its drawing, so the identity it posts under and the
    text Slack shows are untouched."""
    payload = make_payload("first arm")

    assert without_blocks(payload) == {
        "username": "Evals",
        "icon_emoji": ":big_brain:",
        "text": "first arm",
    }


def test_clamp_payload_text_leaves_a_message_slack_will_take_alone() -> None:
    """A report that fits is posted exactly as it was rendered, cap or no cap."""
    payload = make_payload("first arm")

    assert clamp_payload_text(payload) == payload


def test_clamp_payload_text_cuts_an_oversized_report_to_the_cap_on_a_line_boundary() -> None:
    """Slack refuses the whole message past its `text` cap, so a suite whose scores run past it is
    cut down to a report that says so rather than to no report at all."""
    line = "a" * 99
    payload = make_payload("\n".join([line] * 500))
    assert len(payload["text"]) > MAX_SLACK_TEXT_CHARACTERS

    clamped = clamp_payload_text(payload)

    text = clamped["text"]
    assert len(text) <= MAX_SLACK_TEXT_CHARACTERS
    assert text.endswith("\n" + TEXT_CUT_NOTICE)
    # Every line before the notice survived whole, which is what keeps the cut out of the middle of a
    # fixed-width fence's row.
    assert set(text.split("\n")[:-1]) == {line}


def test_clamp_payload_text_leaves_the_blocks_a_cut_message_carries_alone() -> None:
    """The cap is the plain text's own; the blocks say the same thing in a form Slack bounds
    separately, and a message whose blocks render is read from them."""
    payload = make_payload("\n".join(["a" * 99] * 500))

    clamped = clamp_payload_text(payload)

    assert clamped["blocks"] == payload["blocks"]
    assert clamped["username"] == payload["username"]
    assert clamped["icon_emoji"] == payload["icon_emoji"]


def test_post_threads_cuts_an_oversized_report_before_it_goes_out() -> None:
    """The cut is made at posting time, so both the message and its replies are within the cap
    whichever of the two forms Slack ends up taking."""
    oversized = "\n".join(["a" * 99] * 500)
    poster = MockSlackPoster(blocks_rejecting_call_indexes=frozenset({0}))

    outcome = post_report(poster, [make_thread(oversized, (oversized,))])

    assert [len(post.payload["text"]) <= MAX_SLACK_TEXT_CHARACTERS for post in poster.posts] == [True, True, True]
    # The fallback carries the cut text too: dropping the blocks leaves the `text` Slack refused.
    assert "blocks" not in poster.posts[1].payload
    assert poster.posts[1].payload["text"] == poster.posts[0].payload["text"]
    assert (outcome.posted_count, outcome.failed_count) == (2, 0)


def test_read_api_answer_takes_the_message_id_out_of_what_slack_answered() -> None:
    """`ts` is what a reply is threaded under, and it is the only thing in the answer this needs."""
    assert (
        read_api_answer(200, {"ok": True, "ts": "1700000000.000100", "channel": CHANNEL}, None) == "1700000000.000100"
    )


@pytest.mark.parametrize(
    ("answer", "expected_error"),
    [
        # The refusal that is worth retrying without blocks, and one that is not.
        ({"ok": False, "error": "invalid_blocks"}, "invalid_blocks"),
        ({"ok": False, "error": "channel_not_found"}, "channel_not_found"),
        ({"ok": False}, ""),
        ("not an object", ""),
    ],
)
def test_read_api_answer_raises_with_the_code_slack_named(answer: Any, expected_error: str) -> None:
    """The API answers 200 with `ok: false` for everything it refuses, so the body rather than the
    status says whether a message was posted -- and the code it names is what decides whether the
    message is worth posting again without its blocks."""
    with pytest.raises(SlackPostError) as raised:
        read_api_answer(200, answer, None)

    assert raised.value.slack_error == expected_error


@pytest.mark.parametrize(
    ("status_code", "answer", "retry_after_header", "expected_wait"),
    [
        # A 429 whose body Slack served from its edge, with the header and without it, then the
        # same refusal named in a body the API itself answered.
        (429, "too many requests", "12", 12.0),
        (429, "", None, None),
        (429, "", "not a number", None),
        (429, "", "nan", None),
        (200, {"ok": False, "error": SLACK_RATE_LIMITED_ERROR}, "3", 3.0),
    ],
)
def test_read_api_answer_names_a_rate_limit_and_the_wait_slack_asked_for(
    status_code: int, answer: Any, retry_after_header: str | None, expected_wait: float | None
) -> None:
    """A rate limit is the one refusal the status decides rather than the body, since Slack answers
    it before the API sees the message and the body is no JSON at all. The wait is whatever
    `Retry-After` carried, and None where it carried nothing this can read -- `nan` included, which
    parses as a float and would otherwise be waited out forever."""
    with pytest.raises(SlackPostError) as raised:
        read_api_answer(status_code, answer, retry_after_header)

    assert raised.value.slack_error == SLACK_RATE_LIMITED_ERROR
    assert raised.value.retry_after_seconds == expected_wait


def test_check_webhook_answer_carries_the_refusal_a_webhook_puts_in_its_body() -> None:
    """A webhook names its refusal in the body rather than in a code of its own, so that body is what
    the fallback rule reads."""
    check_webhook_answer(200, "ok", None)

    with pytest.raises(SlackPostError) as raised:
        check_webhook_answer(400, "invalid_blocks\n", None)

    assert raised.value.slack_error == "invalid_blocks"


def test_check_webhook_answer_names_a_rate_limit_by_its_status() -> None:
    """A webhook says nothing in the body of a 429, so the status is what names the refusal and the
    `Retry-After` header is the only thing that says how long it wants."""
    with pytest.raises(SlackPostError) as with_header:
        check_webhook_answer(429, "", "5")

    with pytest.raises(SlackPostError) as without_header:
        check_webhook_answer(429, "", None)

    assert (with_header.value.slack_error, with_header.value.retry_after_seconds) == (SLACK_RATE_LIMITED_ERROR, 5.0)
    assert (without_header.value.slack_error, without_header.value.retry_after_seconds) == (
        SLACK_RATE_LIMITED_ERROR,
        None,
    )


def recording_transport(taken: list[httpx.Request], answer: httpx.Response) -> httpx.MockTransport:
    """A stand-in Slack that keeps the request it was handed and answers with one prepared reply.

    The request is what the assertions read: a poster's whole job is the call it makes, and nothing
    else in the module says what goes on the wire.
    """

    def take(request: httpx.Request) -> httpx.Response:
        taken.append(request)
        return answer

    return httpx.MockTransport(take)


def unreachable_transport() -> httpx.MockTransport:
    """A Slack that cannot be reached at all, which httpx raises about rather than answering."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(refuse)


def test_slack_api_poster_posts_the_payload_to_the_channel_as_the_app_its_token_belongs_to() -> None:
    """The channel is named in the body rather than in the payload `ci-report` wrote, and the token
    goes in the header: posted without either, a message reaches nobody."""
    taken: list[httpx.Request] = []
    poster = SlackApiPoster(
        token="xoxb-token",
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport(taken, httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"})),
    )

    message_ts = asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert message_ts == "1700000000.000100"
    assert str(taken[0].url) == SLACK_POST_MESSAGE_URL
    assert taken[0].headers["Authorization"] == "Bearer xoxb-token"
    # Compared whole, so that a top message is pinned as carrying no `thread_ts` key at all.
    assert json.loads(taken[0].content) == {**make_payload("first arm"), "channel": CHANNEL}


def test_slack_api_poster_names_the_message_a_reply_is_threaded_under() -> None:
    """A reply carries the `ts` its own thread's message answered with, which is the whole reason a
    run posts with a bot token."""
    taken: list[httpx.Request] = []
    poster = SlackApiPoster(
        token="xoxb-token",
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport(taken, httpx.Response(200, json={"ok": True, "ts": "1700000000.000200"})),
    )

    asyncio.run(poster.post(CHANNEL, make_payload("scores"), "1700000000.000100"))

    assert json.loads(taken[0].content) == {
        **make_payload("scores"),
        "channel": CHANNEL,
        "thread_ts": "1700000000.000100",
    }


def test_slack_api_poster_raises_with_the_code_slack_named_in_a_200() -> None:
    """The API refuses with `ok: false` and a 200, so a poster that read the status alone would
    report every refusal as a message that went out."""
    poster = SlackApiPoster(
        token="xoxb-token",
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport([], httpx.Response(200, json={"ok": False, "error": "channel_not_found"})),
    )

    with pytest.raises(SlackPostError) as raised:
        asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert raised.value.slack_error == "channel_not_found"


def test_slack_api_poster_carries_the_wait_a_429_asked_for() -> None:
    """A rate limit comes as a 429 whose body is no JSON at all, and the `Retry-After` header is the
    only thing that says how long to leave the channel alone before posting again."""
    poster = SlackApiPoster(
        token="xoxb-token",
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport(
            [], httpx.Response(429, text="too many requests", headers={RETRY_AFTER_HEADER: "2"})
        ),
    )

    with pytest.raises(SlackPostError) as raised:
        asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert (raised.value.slack_error, raised.value.retry_after_seconds) == (SLACK_RATE_LIMITED_ERROR, 2.0)


def test_slack_api_poster_reports_a_slack_it_could_not_reach_at_all() -> None:
    """An outage is not a refusal with a code, and it is reported as one Slack named nothing about:
    an empty code is what keeps the message from being posted again as text or after a wait."""
    poster = SlackApiPoster(
        token="xoxb-token", timeout_seconds=SLACK_POST_TIMEOUT_SECONDS, transport=unreachable_transport()
    )

    with pytest.raises(SlackPostError) as raised:
        asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert (raised.value.slack_error, raised.value.retry_after_seconds) == ("", None)
    assert "Slack could not be reached" in str(raised.value)


def test_webhook_poster_posts_the_payload_as_it_came_and_answers_with_no_message_id() -> None:
    """A webhook is bound to the channel it was created for, so the channel it is handed goes
    nowhere -- and it answers `ok` rather than a `ts`, which is why nothing can be threaded under
    what it posted."""
    taken: list[httpx.Request] = []
    poster = WebhookPoster(
        webhook_url=WEBHOOK_URL,
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport(taken, httpx.Response(200, text="ok")),
    )

    message_ts = asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert message_ts == NO_MESSAGE_TS
    assert str(taken[0].url) == WEBHOOK_URL
    assert json.loads(taken[0].content) == make_payload("first arm")


def test_webhook_poster_carries_the_refusal_a_webhook_puts_in_its_body() -> None:
    """A webhook names its refusal in the body of a 4xx rather than in a code of its own, so the body
    is what the fallback rule reads."""
    poster = WebhookPoster(
        webhook_url=WEBHOOK_URL,
        timeout_seconds=SLACK_POST_TIMEOUT_SECONDS,
        transport=recording_transport([], httpx.Response(400, text="invalid_blocks")),
    )

    with pytest.raises(SlackPostError) as raised:
        asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert raised.value.slack_error == "invalid_blocks"


def test_webhook_poster_reports_a_slack_it_could_not_reach_at_all() -> None:
    """An outage through the webhook is as codeless as one through the API, and for the same reason:
    an empty code is what keeps the message from being posted again as text or after a wait."""
    poster = WebhookPoster(
        webhook_url=WEBHOOK_URL, timeout_seconds=SLACK_POST_TIMEOUT_SECONDS, transport=unreachable_transport()
    )

    with pytest.raises(SlackPostError) as raised:
        asyncio.run(poster.post(CHANNEL, make_payload("first arm"), None))

    assert (raised.value.slack_error, raised.value.retry_after_seconds) == ("", None)
    assert "Slack could not be reached" in str(raised.value)


def test_the_posters_keep_their_credential_out_of_everything_that_prints() -> None:
    """A token and a webhook URL are both secrets, and a poster is logged, repr'd and dumped wherever
    something goes wrong -- which is exactly when a credential must not be in the line."""
    api_poster = SlackApiPoster(token="xoxb-secret-token", timeout_seconds=SLACK_POST_TIMEOUT_SECONDS)
    webhook_poster = WebhookPoster(
        webhook_url="https://hooks.slack.com/services/secret", timeout_seconds=SLACK_POST_TIMEOUT_SECONDS
    )

    assert "xoxb-secret-token" not in repr(api_poster)
    assert "secret" not in repr(webhook_poster)
    # Serializing a model is how a structured log line carries it, and is the one spelling that
    # writes a field's value out rather than its `repr`.
    assert "xoxb-secret-token" not in api_poster.model_dump_json()
    assert "secret" not in webhook_poster.model_dump_json()
    assert api_poster.token.get_secret_value() == "xoxb-secret-token"


def test_run_post_slack_report_refuses_a_channel_that_is_not_a_channel(tmp_path: Path) -> None:
    """A `U` id is a person: Slack delivers to it through its system user and will not thread under
    what it delivered, so the whole report would arrive as a wall of unrelated messages. Refused
    before anything is posted, which is the only point at which this command may still fail."""
    payloads_path = write_payloads(tmp_path / "payloads.json", (make_thread("first arm", ()),))
    poster = MockSlackPoster()

    with pytest.raises(click.UsageError):
        run_post_slack_report(poster, channel="U0BUFUVU0T0", payloads_path=payloads_path, clock=ManualClock())

    assert poster.posts == []


def test_run_post_slack_report_warns_and_posts_nothing_without_a_credential(tmp_path: Path) -> None:
    """A notification must never turn a run red, so a night with no credential in Vault is a warning
    and the run's step summary is the whole report."""
    payloads_path = write_payloads(tmp_path / "payloads.json", (make_thread("first arm", ()),))

    # `None` deletes the variable for the duration of the call. An empty mapping would override
    # nothing, and a developer holding either credential would post to Slack from a unit test.
    result = CliRunner().invoke(
        main,
        ["post-slack-report", "--payloads", str(payloads_path), "--channel", CHANNEL],
        env={SLACK_BOT_TOKEN_ENV_VAR: None, SLACK_WEBHOOK_ENV_VAR: None},
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "::warning::neither {} nor {} is set -- the run summary was not posted to Slack".format(
            SLACK_BOT_TOKEN_ENV_VAR, SLACK_WEBHOOK_ENV_VAR
        )
    ]


def test_poster_from_environment_prefers_the_bot_token_over_the_webhook() -> None:
    """The token is the only credential Slack answers with a message id, and the replies -- where
    every score behind a grid lives -- can only be threaded under one. So a run holding both posts as
    the app, and one holding neither posts nothing rather than failing."""
    both = poster_from_environment(
        {SLACK_BOT_TOKEN_ENV_VAR: "xoxb-token", SLACK_WEBHOOK_ENV_VAR: "https://hooks.slack.test/services/x"},
        SLACK_POST_TIMEOUT_SECONDS,
    )
    webhook_only = poster_from_environment(
        {SLACK_WEBHOOK_ENV_VAR: "https://hooks.slack.test/services/x"}, SLACK_POST_TIMEOUT_SECONDS
    )

    assert isinstance(both, SlackApiPoster)
    assert both.token.get_secret_value() == "xoxb-token"
    assert isinstance(webhook_only, WebhookPoster)
    assert webhook_only.webhook_url.get_secret_value() == "https://hooks.slack.test/services/x"
    assert poster_from_environment({}, SLACK_POST_TIMEOUT_SECONDS) is None


def test_run_post_slack_report_refuses_a_payloads_file_that_is_not_a_report(tmp_path: Path) -> None:
    """A file this cannot read is a defect in whatever wrote it rather than anything Slack did, so it
    is reported as the bad input it is instead of being posted around."""
    payloads_path = tmp_path / "payloads.json"
    payloads_path.write_text(json.dumps([{"text": "a payload of the wrong shape"}]))
    poster = MockSlackPoster()

    with pytest.raises(click.BadParameter):
        run_post_slack_report(poster, channel=CHANNEL, payloads_path=payloads_path, clock=ManualClock())

    assert poster.posts == []


def test_run_post_slack_report_refuses_a_payloads_file_that_is_not_text(tmp_path: Path) -> None:
    """A file whose bytes are not the UTF-8 the report is written in is as unreadable as one holding
    the wrong shape, and is refused the same way rather than taking the job down with it."""
    payloads_path = tmp_path / "payloads.json"
    payloads_path.write_bytes(b"\xff\xfe not utf-8")
    poster = MockSlackPoster()

    with pytest.raises(click.BadParameter):
        run_post_slack_report(poster, channel=CHANNEL, payloads_path=payloads_path, clock=ManualClock())

    assert poster.posts == []


def test_run_post_slack_report_takes_a_thread_that_names_no_replies(tmp_path: Path) -> None:
    """The one-line notice the workflow writes in place of a report that could not be rendered is a
    message with nothing under it, and it has to post like any other thread."""
    payloads_path = tmp_path / "payloads.json"
    payloads_path.write_text(json.dumps([{"message": make_payload("the report could not be rendered")}]))
    poster = MockSlackPoster()

    run_post_slack_report(poster, channel=CHANNEL, payloads_path=payloads_path, clock=ManualClock())

    assert [post.payload["text"] for post in poster.posts] == ["the report could not be rendered"]
