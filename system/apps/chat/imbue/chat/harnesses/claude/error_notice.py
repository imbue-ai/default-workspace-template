"""Read what a synthetic Claude Code message says went wrong, off the record's own stamp.

Claude Code writes a failed turn as a synthetic assistant message whose text is the error
the user reads, and marks the RECORD with what happened: ``isApiErrorMessage`` (this
message wraps a failed request), ``apiErrorStatus`` (the HTTP status, when there was one),
and ``error`` (its own normalized kind).

Reading those beats matching the prose, which is all :mod:`error_patterns` could do. Only
the failures Claude Code happens to phrase as ``API Error: <status> ...`` match a regex,
and its whole limit family never does: "You've hit your monthly spend limit · raise it at
claude.ai/settings/usage", "You've hit your session limit · resets 2:50pm", "You're out of
usage credits." each carry ``error: "rate_limit"`` and ``apiErrorStatus: 429`` under
wording with no status anywhere in it -- so they rendered as ordinary assistant prose, as
if the agent had said them, rather than as the failed turn they are.

``error``'s vocabulary is Claude Code's own published one -- its SDK schema and its
``StopFailure`` hook matcher list the same set::

    authentication_failed  oauth_org_not_allowed  account_on_hold  billing_error
    rate_limit  overloaded  invalid_request  model_not_found  server_error
    max_output_tokens  unknown

A kind this module does not name still renders as a failure; it just gets no
kind-specific wording, which is the right way for the set to grow.
"""

from typing import Any

from imbue.chat.harnesses.auth_errors import is_auth_error_text
from imbue.chat.harnesses.error_patterns import classify_api_error
from imbue.chat.harnesses.error_patterns import kind_for_status
from imbue.imbue_common.frozen_model import FrozenModel

# The kinds whose only way forward is different credentials. They belong to the auth
# family (which has its own recovery surface) rather than the API-error one, for the
# reason :mod:`auth_errors` gives: none is an authentication failure in the HTTP sense,
# but a spent balance and a rejected token are the same dead end for the user. This is
# also what already happens by prose -- "Credit balance is too low", the text the auth
# vocabulary claims, is exactly the text Claude Code stamps ``billing_error`` on.
_AUTH_ERROR_KINDS: frozenset[str] = frozenset(
    {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error"}
)

# Claude Code's kind -> our normalized kind, for a failure that carries no HTTP status.
# ``server_error`` is deliberately absent: Claude Code also stamps it on failures that
# never reached a server (a laptop that slept mid-response, a DNS failure), so without a
# 5xx status to confirm it, mapping it to ``api_error`` would print "the model provider's
# servers hit an error" over a local one.
_KIND_BY_CLAUDE_ERROR: dict[str, str] = {
    "rate_limit": "rate_limit",
    "overloaded": "overloaded",
    "invalid_request": "invalid_request",
    "model_not_found": "not_found",
}


class ErrorNotice(FrozenModel):
    """What one synthetic Claude Code message says went wrong, as the wire's error fields.

    The two families are mutually exclusive: a message offering both a sign-in action and
    an "it's the provider's fault, retry" note would give the user contradictory next steps.
    """

    # The credential is the problem: renders with the sign-in / switch-provider action.
    is_auth_error: bool = False
    # The turn failed against the model API: renders as a failure rather than as prose.
    is_api_error: bool = False
    # Normalized kind, for the wording of the provider-fault note. None when the failure is
    # real but unclassified, which still renders -- just without a named cause.
    api_error_kind: str | None = None


_NO_ERROR = ErrorNotice()


def classify_error_notice(raw: dict[str, Any], text: str) -> ErrorNotice:
    """Classify one synthetic Claude Code message from its record ``raw`` and its ``text``.

    Callers must have established that the message is a framework notice (the synthetic
    model): ``text`` is matched as a fallback, and an agent discussing a credential or
    quoting an error body says these things in ordinary prose.
    """
    # Narrow both stamp fields here, at the boundary with the third-party record, so
    # everything below is typed. ``error`` is read as a lookup key, so a stray list or dict
    # there would raise inside the watcher thread and wedge the read path (see
    # ``_parse_assistant_message`` on the null-message guard).
    raw_kind = raw.get("error")
    claude_kind = raw_kind if isinstance(raw_kind, str) else ""
    raw_status = raw.get("apiErrorStatus")
    status = raw_status if isinstance(raw_status, int) else None

    # The stamp decides the auth question only in the POSITIVE direction: a kind Claude Code
    # names as a credential dead end is one, and the prose still gets its say otherwise. It
    # is deliberately NOT a veto, because Claude Code's `invalid_request` bucket is not clean
    # -- it stamps that on a dozen credential problems, of which the prose currently rescues
    # two: "Invalid API key - Fix external API key" and the gateway's "Authentication error".
    # A veto would send those to the plain API-error surface with no way forward. (The other
    # ten land there already -- "Your organization has disabled API key authentication - ...
    # run /login" and its siblings are a gap in the shared auth vocabulary, not in this
    # precedence rule.)
    if claude_kind in _AUTH_ERROR_KINDS or is_auth_error_text(text):
        return ErrorNotice(is_auth_error=True)
    kind = kind_for_status(status) or _KIND_BY_CLAUDE_ERROR.get(claude_kind) or classify_api_error(text)
    if kind is None and raw.get("isApiErrorMessage") is not True:
        return _NO_ERROR
    return ErrorNotice(is_api_error=True, api_error_kind=kind)
