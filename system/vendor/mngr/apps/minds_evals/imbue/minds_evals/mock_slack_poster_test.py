from collections.abc import Mapping
from typing import Any

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_evals.errors import SlackPostError
from imbue.minds_evals.slack_post import NO_MESSAGE_TS
from imbue.minds_evals.slack_post import SLACK_RATE_LIMITED_ERROR
from imbue.minds_evals.slack_post import SlackPoster


class RecordedPost(FrozenModel):
    """One call a mock poster took, in full: what was posted, where, and under what."""

    channel: str = Field(description="The channel it was posted to")
    payload: dict[str, Any] = Field(description="The payload as the poster received it")
    thread_ts: str = Field(description="The message it was threaded under; empty when it was not threaded")


class MockSlackPoster(SlackPoster):
    """A Slack that records what it was given and answers however the test needs it to.

    A `ts` per accepted message, so a reply's threading can be read off the record; the refusals are
    configured by payload index across the whole pass, counting a fallback retry as a call of its own.

    A rate limit is configured as a count rather than by index, because that is how the real one
    behaves: the first calls of a burst are refused and a later one is taken, wherever in the pass
    the burst falls. Such a call is recorded like any other, so it shifts the indexes after it.
    """

    posts: list[RecordedPost] = Field(default_factory=list, description="Every call, in order")
    blocks_rejecting_call_indexes: frozenset[int] = Field(
        default=frozenset(), description="Calls answered as a message whose blocks Slack will not render"
    )
    failing_call_indexes: frozenset[int] = Field(
        default=frozenset(), description="Calls answered with a refusal nothing can retry"
    )
    rate_limited_answer_count: int = Field(
        default=0, description="How many calls are answered with a rate limit before one is taken"
    )
    rate_limit_retry_after_seconds: float | None = Field(
        default=None, description="The `Retry-After` those refusals name; none where Slack sent no header"
    )
    given_rate_limit_count: int = Field(default=0, description="Rate limits answered so far, over the whole pass")
    is_threading_supported: bool = Field(
        default=True, description="Whether an accepted message answers with a `ts` a reply can go under"
    )

    async def post(self, channel: str, payload: Mapping[str, Any], thread_ts: str | None) -> str:
        call_index = len(self.posts)
        self.posts.append(RecordedPost(channel=channel, payload=dict(payload), thread_ts=thread_ts or NO_MESSAGE_TS))
        if self.given_rate_limit_count < self.rate_limited_answer_count:
            self.given_rate_limit_count += 1
            raise SlackPostError(
                SLACK_RATE_LIMITED_ERROR, "Slack rate-limited the message", self.rate_limit_retry_after_seconds
            )
        if call_index in self.failing_call_indexes:
            raise SlackPostError("channel_not_found", "Slack refused the message: channel_not_found")
        if call_index in self.blocks_rejecting_call_indexes:
            raise SlackPostError("invalid_blocks", "Slack refused the message: invalid_blocks")
        return NO_MESSAGE_TS if not self.is_threading_supported else "17000000.{:06d}".format(call_index)
