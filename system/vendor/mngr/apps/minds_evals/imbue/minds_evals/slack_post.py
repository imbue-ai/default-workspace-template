"""Post the report of a scheduled run to Slack, as threads.

The notification is the whole of what anyone reads about a night, so nothing here fails a run: a
refusal, an outage and a missing credential are all warnings, and whatever could not be posted is in
the step summary the same job writes.

Two transports, and they are not equivalent. `chat.postMessage` with a bot token answers with the
posted message's `ts`, which is what a reply is threaded under; an incoming webhook answers with `ok`
and nothing else, so a run posting through it gets the top messages alone and says so. The webhook is
the fallback rather than the other way round: the replies are where every score behind a grid lives.
"""

import math
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.clock import ClockInterface
from imbue.minds_evals.errors import SlackPostError

SLACK_POST_MESSAGE_URL: Final[str] = "https://slack.com/api/chat.postMessage"

SLACK_BOT_TOKEN_ENV_VAR: Final[str] = "SLACK_MINDS_EVALS_BOT_TOKEN"
SLACK_WEBHOOK_ENV_VAR: Final[str] = "SLACK_MINDS_EVALS_WEBHOOK"

# What a channel id starts with. A `U` id is a person, which Slack delivers to through its system
# user and refuses to thread under, so the report would arrive as a wall of unrelated messages.
SLACK_CHANNEL_ID_PREFIX: Final[str] = "C"

# How a GitHub Actions step annotates a line of its log. Emitted by whatever runs this, so that a
# reader of the run sees what became of the notification without opening the step.
GITHUB_WARNING_PREFIX: Final[str] = "::warning::"

# Generous, because a post that takes this long is a Slack outage rather than a slow message, and
# every failure here is a warning the run carries on past.
SLACK_POST_TIMEOUT_SECONDS: Final[float] = 30.0

# What Slack answers when it will not render a payload's blocks. The message's own `text` says
# everything its blocks do, so such a refusal is retried without them rather than losing the report.
BLOCK_REFUSAL_ERRORS: Final[frozenset[str]] = frozenset({"invalid_blocks", "invalid_blocks_format"})

BLOCKS_PAYLOAD_KEY: Final[str] = "blocks"

TEXT_PAYLOAD_KEY: Final[str] = "text"

# Slack refuses a `chat.postMessage` whose `text` runs past this, and refuses the whole message with
# it -- a refusal neither of the two retries here answers, since dropping the blocks leaves the same
# oversized text. Every payload's `text` is the whole report rather than a caption, and a scoring
# reply's fixed-width fence pads each cell out to its column's width, so a wide suite reaches this
# even though the tables themselves are budgeted against Slack's cell-character cap.
MAX_SLACK_TEXT_CHARACTERS: Final[int] = 40_000

# What a message that was cut ends with. The run's step summary is written from the rendered report
# file rather than from what was posted, so it carries the whole of what came off here.
TEXT_CUT_NOTICE: Final[str] = "... (cut to Slack's limit; the whole of it is in the run summary)"

# What a transport that cannot say where a message landed answers in place of a `ts`.
NO_MESSAGE_TS: Final[str] = ""

HTTP_ERROR_STATUS: Final[int] = 400

HTTP_TOO_MANY_REQUESTS_STATUS: Final[int] = 429

# What Slack answers when a channel has taken messages faster than it allows: 429, and an `error` of
# this where it answers a body at all. `chat.postMessage` allows about a message a second per
# channel and a report posts a thread's message and its replies back to back, so a burst of them is
# refused rather than queued -- which is why this refusal is waited out rather than reported.
SLACK_RATE_LIMITED_ERROR: Final[str] = "ratelimited"

# The header Slack names the wait in, in whole seconds.
RETRY_AFTER_HEADER: Final[str] = "Retry-After"

# What to wait when Slack refused a burst without saying how long. One second is the rate
# `chat.postMessage` allows per channel, so it is the shortest wait that can be expected to clear.
DEFAULT_RATE_LIMIT_WAIT_SECONDS: Final[float] = 1.0

# The longest one message waits, however large the `Retry-After` Slack sends. Past this the channel
# is throttling the whole report rather than this message, and a notification that holds the run's
# job open costs more than the message it is waiting to post.
MAX_RATE_LIMIT_WAIT_SECONDS: Final[float] = 30.0

# How many times one message is posted before a rate limit is reported like any other refusal. Two
# waits clear the burst a thread's own messages make; a third refusal is a channel under load from
# somewhere else, which waiting longer here does not fix.
MAX_RATE_LIMITED_ATTEMPT_COUNT: Final[int] = 3


class SlackThreadPayload(FrozenModel):
    """One thread of the report as `ci-report` wrote it: a message, and what goes under it."""

    message: dict[str, Any] = Field(description="The payload posted to the channel")
    # Defaulted, so that a one-line notice written in place of a report that could not be rendered is
    # a thread like any other rather than a file this command refuses.
    replies: tuple[dict[str, Any], ...] = Field(default=(), description="The payloads posted in its thread, in order")


class SlackPostOutcome(FrozenModel):
    """What one posting pass did, and everything about it a workflow reader should see.

    The warnings are returned rather than printed here, because how they have to be printed is the
    caller's business: a GitHub step annotates them, and anyone else reads them as log lines.
    """

    posted_count: int = Field(description="Messages Slack accepted, the ones that fell back included")
    fallback_count: int = Field(description="Messages that went as plain text because Slack refused their blocks")
    failed_count: int = Field(description="Messages Slack would not take at all")
    warnings: tuple[str, ...] = Field(description="What went wrong, in the order it happened")


@pure
def read_retry_after_seconds(retry_after_header: str | None) -> float | None:
    """How long Slack asked to be left alone, out of the `Retry-After` it sent.

    None where it sent none, and where it sent something that is not a finite number of seconds: the
    wait falls back to the documented default rather than to a figure read out of an unrecognised
    header. Finite rather than merely parseable, because `float` reads `nan` and `inf` as numbers and
    a `nan` wait is one no amount of time elapses.
    """
    if retry_after_header is None:
        return None
    try:
        seconds = float(retry_after_header.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


@pure
def read_api_answer(status_code: int, answer: Any, retry_after_header: str | None) -> str:
    """The posted message's `ts`, out of what `chat.postMessage` answered.

    The API answers 200 with `ok: false` for everything it refuses, so the body rather than the status
    is what says whether a message was posted. The one exception is a rate limit, which comes as a 429
    whose body may be no JSON at all, so the status decides that one. Raises SlackPostError naming the
    code Slack gave, which is what decides whether the message is posted again -- without its blocks,
    or after the wait this reads off the header.
    """
    if status_code == HTTP_TOO_MANY_REQUESTS_STATUS:
        raise SlackPostError(
            SLACK_RATE_LIMITED_ERROR,
            "Slack rate-limited the message",
            read_retry_after_seconds(retry_after_header),
        )
    if not isinstance(answer, Mapping):
        raise SlackPostError("", "Slack answered {} with something that is not an object".format(status_code))
    if not answer.get("ok"):
        error = str(answer.get("error", ""))
        raise SlackPostError(
            error,
            "Slack refused the message: {}".format(error or status_code),
            read_retry_after_seconds(retry_after_header) if error == SLACK_RATE_LIMITED_ERROR else None,
        )
    return str(answer.get("ts", NO_MESSAGE_TS))


@pure
def check_webhook_answer(status_code: int, body_text: str, retry_after_header: str | None) -> None:
    """Raises SlackPostError when a webhook refused the message.

    A webhook names its refusal in the body rather than in a code of its own -- `invalid_blocks` is
    what it says about a block type it will not render -- so the body is carried as the error code.
    A rate limit is the exception: it is a 429 whose body says nothing, so the status names it and
    the `Retry-After` header says how long it wants.
    """
    if status_code < HTTP_ERROR_STATUS:
        return
    if status_code == HTTP_TOO_MANY_REQUESTS_STATUS:
        raise SlackPostError(
            SLACK_RATE_LIMITED_ERROR,
            "Slack rate-limited the message",
            read_retry_after_seconds(retry_after_header),
        )
    refusal = body_text.strip()
    raise SlackPostError(refusal, "Slack refused the message: {}".format(refusal or status_code))


@pure
def rate_limit_wait_seconds(retry_after_seconds: float | None) -> float:
    """How long to leave a rate-limited channel alone before posting the message again.

    Slack's own figure where it sent a usable one, and the default where it did not. Capped, so that
    a `Retry-After` of minutes costs the attempt it is spent on rather than the run's whole job.
    """
    if retry_after_seconds is None or retry_after_seconds <= 0:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS
    return min(retry_after_seconds, MAX_RATE_LIMIT_WAIT_SECONDS)


class SlackPoster(MutableModel, ABC):
    """Posts one message to Slack."""

    @abstractmethod
    async def post(self, channel: str, payload: Mapping[str, Any], thread_ts: str | None) -> str:
        """Post one payload and return the posted message's `ts`, empty where the transport cannot
        say. Raises SlackPostError for anything Slack refuses or does not answer."""


class SlackApiPoster(SlackPoster):
    """Posts as the app the bot token belongs to, which is the only way a reply can be threaded."""

    # An httpx transport is not a type pydantic knows how to validate, and it is held rather than
    # built per call so that a test can hand one in.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    token: SecretStr = Field(frozen=True, description="The bot token the post authenticates with")
    timeout_seconds: float = Field(frozen=True, description="How long one post may take")
    transport: httpx.AsyncBaseTransport | None = Field(
        frozen=True,
        default=None,
        description="What carries the request; None is httpx's own, and a test hands an `httpx.MockTransport` here",
    )

    async def post(self, channel: str, payload: Mapping[str, Any], thread_ts: str | None) -> str:
        body: dict[str, Any] = {**payload, "channel": channel}
        if thread_ts:
            body["thread_ts"] = thread_ts
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    SLACK_POST_MESSAGE_URL,
                    json=body,
                    headers={"Authorization": "Bearer {}".format(self.token.get_secret_value())},
                )
        except httpx.HTTPError as exc:
            raise SlackPostError("", "Slack could not be reached: {}".format(exc)) from exc
        try:
            answer: Any = response.json()
        except ValueError:
            # A refusal Slack serves before the API sees it -- a rate limit, a gateway error -- has
            # no JSON body at all, and its status is what names it, so the body is handed on as it
            # came rather than being raised about here.
            answer = response.text
        return read_api_answer(response.status_code, answer, response.headers.get(RETRY_AFTER_HEADER))


class WebhookPoster(SlackPoster):
    """Posts through an incoming webhook, which carries no identity and cannot thread.

    A webhook answers `ok` and no `ts`, so nothing can be posted under what it posted; the caller is
    what decides that a thread's replies are skipped rather than posted beside its message.
    """

    # An httpx transport is not a type pydantic knows how to validate, and it is held rather than
    # built per call so that a test can hand one in.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    webhook_url: SecretStr = Field(frozen=True, description="The incoming webhook the post goes to")
    timeout_seconds: float = Field(frozen=True, description="How long one post may take")
    transport: httpx.AsyncBaseTransport | None = Field(
        frozen=True,
        default=None,
        description="What carries the request; None is httpx's own, and a test hands an `httpx.MockTransport` here",
    )

    async def post(self, channel: str, payload: Mapping[str, Any], thread_ts: str | None) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(self.webhook_url.get_secret_value(), json=dict(payload))
        except httpx.HTTPError as exc:
            raise SlackPostError("", "Slack could not be reached: {}".format(exc)) from exc
        check_webhook_answer(response.status_code, response.text, response.headers.get(RETRY_AFTER_HEADER))
        return NO_MESSAGE_TS


@pure
def clamp_payload_text(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The same payload with a `text` Slack will take.

    The cut lands on a line boundary, because a raw character cut ends inside a fixed-width fence or
    a `:emoji:` token, which then renders as literal text beside the notice. The first line is kept
    whatever it says, so a payload of one enormous line still says something, and is the only line
    cut mid-word. The notice and the newline before it come out of the budget rather than sitting on
    top of it, so what is returned is never over the cap.

    Everything else the payload holds is carried through untouched: the blocks say the same thing in
    a form Slack bounds separately, and a message whose blocks render is read from them.
    """
    text = payload.get(TEXT_PAYLOAD_KEY)
    if not isinstance(text, str) or len(text) <= MAX_SLACK_TEXT_CHARACTERS:
        return dict(payload)
    kept_budget = MAX_SLACK_TEXT_CHARACTERS - len(TEXT_CUT_NOTICE) - 1
    lines = text.split("\n")
    kept = [lines[0][:kept_budget]]
    length = len(kept[0])
    for line in lines[1:]:
        if length + len(line) + 1 > kept_budget:
            break
        kept.append(line)
        length += len(line) + 1
    return {**payload, TEXT_PAYLOAD_KEY: "\n".join([*kept, TEXT_CUT_NOTICE])}


@pure
def without_blocks(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The same message as plain text. Its `text` is the whole report rather than a caption, so what
    is posted still says everything the blocks would have."""
    return {key: value for key, value in payload.items() if key != BLOCKS_PAYLOAD_KEY}


class PostAttempt(FrozenModel):
    """What became of one message: where a reply to it would go, and what is worth warning about."""

    message_ts: str = Field(description="The posted message's `ts`; empty when it was not posted or cannot be told")
    is_posted: bool = Field(description="Whether Slack took the message, in either of its two forms")
    is_fallback: bool = Field(description="Whether it went as plain text because Slack refused its blocks")
    warnings: tuple[str, ...] = Field(description="What a workflow reader should see about this message")


async def post_past_rate_limit(
    poster: SlackPoster,
    clock: ClockInterface,
    channel: str,
    payload: Mapping[str, Any],
    thread_ts: str | None,
    label: str,
) -> str:
    """One message, posted again after waiting out a rate limit.

    A report posts a thread's message and its replies back to back, which is faster than the message
    a second per channel `chat.postMessage` allows, so a burst is refused rather than queued and the
    wait is all such a refusal needs. Every other refusal, and a rate limit that outlasts
    MAX_RATE_LIMITED_ATTEMPT_COUNT attempts, is raised for the caller to report like any other.
    """
    for _ in range(MAX_RATE_LIMITED_ATTEMPT_COUNT - 1):
        try:
            return await poster.post(channel, payload, thread_ts)
        except SlackPostError as exc:
            if exc.slack_error != SLACK_RATE_LIMITED_ERROR:
                raise
            wait_seconds = rate_limit_wait_seconds(exc.retry_after_seconds)
            logger.warning("Slack rate-limited {}; posting it again in {} second(s)", label, wait_seconds)
            await clock.sleep(wait_seconds)
    return await poster.post(channel, payload, thread_ts)


async def post_message_with_fallback(
    poster: SlackPoster,
    clock: ClockInterface,
    channel: str,
    payload: Mapping[str, Any],
    thread_ts: str | None,
    label: str,
) -> PostAttempt:
    """One message, retried as plain text when Slack refuses its blocks.

    A refusal is most likely a block type the workspace will not render, which the report cannot know
    about in advance -- so the retry is what keeps a message that cannot be drawn from being a message
    nobody gets. Anything else Slack says is the end of that message.

    The text is cut to Slack's cap here, before either form of the message goes out: the fallback
    keeps the same `text`, so a cut made afterwards would come too late for the retry to help.
    """
    posted_payload = clamp_payload_text(payload)
    try:
        message_ts = await post_past_rate_limit(poster, clock, channel, posted_payload, thread_ts, label)
        return PostAttempt(message_ts=message_ts, is_posted=True, is_fallback=False, warnings=())
    except SlackPostError as exc:
        if exc.slack_error not in BLOCK_REFUSAL_ERRORS:
            warning = "Slack would not take {}: {}".format(label, exc)
            logger.warning(warning)
            return PostAttempt(message_ts=NO_MESSAGE_TS, is_posted=False, is_fallback=False, warnings=(warning,))
        refusal = "Slack rejected the blocks of {}; posting its text instead".format(label)
        logger.warning(refusal)
    try:
        message_ts = await post_past_rate_limit(
            poster, clock, channel, without_blocks(posted_payload), thread_ts, label
        )
    except SlackPostError as exc:
        warning = "Slack would not take the text of {} either: {}".format(label, exc)
        logger.warning(warning)
        return PostAttempt(message_ts=NO_MESSAGE_TS, is_posted=False, is_fallback=False, warnings=(refusal, warning))
    return PostAttempt(message_ts=message_ts, is_posted=True, is_fallback=True, warnings=(refusal,))


async def post_one_thread(
    poster: SlackPoster, clock: ClockInterface, channel: str, thread: SlackThreadPayload, label: str
) -> tuple[PostAttempt, ...]:
    """Every attempt one thread takes: its message, then its replies under it.

    A message with no `ts` -- posted through a webhook, or refused outright -- keeps its replies out
    of the channel rather than having them posted as messages of their own, where a table of scores
    with no message over it would read as a report about nothing.
    """
    attempt = await post_message_with_fallback(poster, clock, channel, thread.message, None, label)
    if not thread.replies:
        return (attempt,)
    if not attempt.message_ts:
        # A message Slack refused outright is already accounted for by the warning that names the
        # refusal; only a message that was posted and still has no id -- a webhook's -- needs this.
        if not attempt.is_posted:
            return (attempt,)
        skipped = "{} could not be threaded, so its {} repl{} were not posted".format(
            label, len(thread.replies), "y" if len(thread.replies) == 1 else "ies"
        )
        logger.warning(skipped)
        return (attempt.model_copy_update(to_update(attempt.field_ref().warnings, (*attempt.warnings, skipped))),)
    return (
        attempt,
        *[
            await post_message_with_fallback(
                poster, clock, channel, reply, attempt.message_ts, "{} reply {}".format(label, index + 1)
            )
            for index, reply in enumerate(thread.replies)
        ],
    )


def poster_from_environment(environ: Mapping[str, str], timeout_seconds: float) -> SlackPoster | None:
    """How a run posts: as the app the bot token belongs to, through the webhook, or not at all.

    The token wins because it is the only credential Slack answers with a message id, and the replies
    -- where every score behind a grid lives -- can only be threaded under one. Both are read from the
    environment rather than taken as arguments of a command: a flag is in the process table and in the
    run's own log line.
    """
    token = environ.get(SLACK_BOT_TOKEN_ENV_VAR, "")
    if token:
        return SlackApiPoster(token=SecretStr(token), timeout_seconds=timeout_seconds)
    webhook_url = environ.get(SLACK_WEBHOOK_ENV_VAR, "")
    if webhook_url:
        return WebhookPoster(webhook_url=SecretStr(webhook_url), timeout_seconds=timeout_seconds)
    return None


async def post_threads(
    poster: SlackPoster, clock: ClockInterface, channel: str, threads: Sequence[SlackThreadPayload]
) -> SlackPostOutcome:
    """Post every thread of a report, and say what became of them all.

    Nothing here raises for anything Slack does, and a thread that could not be posted costs the
    threads after it nothing: each of them is a whole report of one arm, and losing the rest of a
    night to the first refusal is exactly what this must not do.

    One thread at a time, and one message at a time within it: a reply goes under a `ts` the message
    before it answered with, and posting the report any faster is what the channel's rate limit is
    there to refuse.
    """
    attempts: list[PostAttempt] = []
    for index, thread in enumerate(threads):
        attempts.extend(await post_one_thread(poster, clock, channel, thread, "report message {}".format(index + 1)))
    return SlackPostOutcome(
        posted_count=sum(1 for attempt in attempts if attempt.is_posted),
        fallback_count=sum(1 for attempt in attempts if attempt.is_fallback),
        failed_count=sum(1 for attempt in attempts if not attempt.is_posted),
        warnings=tuple(warning for attempt in attempts for warning in attempt.warnings),
    )
