"""Classify a model API error from Claude assistant transcript text.

When a request to the model API fails, Claude Code writes the failure into the
transcript as a synthetic assistant message -- a surface line like
``API Error: 529 Overloaded`` (model ``<synthetic>``), sometimes carrying the raw
error JSON ``{"type": "overloaded_error", ...}``. This module recognizes those
and maps them to a normalized error kind, plus whether the failure is the model
provider's fault (a 5xx / overloaded on their side) rather than something in the
request we sent.

Kept in a dedicated module (sibling of :mod:`auth_patterns`) so the patterns can
grow without touching parser logic.

Auth-family failures (401 / invalid key / budget) are handled separately by
:mod:`auth_patterns` -- they have their own recovery surface (the sign-in modal)
-- so they are deliberately NOT reclassified here (401 is absent from the tables
below, and ``authentication_error`` has no kind).

The kind set and the provider-fault split are ordinary HTTP semantics, so they
are harness-agnostic; only the two surface-form regexes are Claude-shaped. A
second harness that surfaces provider errors differently classifies them in its
own parser and stamps the same event fields (``is_api_error`` /
``api_error_kind`` / ``is_provider_fault``), which is the shared contract the
frontend reads.
"""

from __future__ import annotations

import re

# HTTP status -> normalized kind. Sourced from the Anthropic API errors reference
# (platform.claude.com/docs/en/api/errors). 401 is intentionally omitted -- auth
# is handled by auth_patterns.py.
_STATUS_KINDS: dict[str, str] = {
    "400": "invalid_request",
    "403": "permission",
    "404": "not_found",
    "413": "request_too_large",
    "429": "rate_limit",
    "500": "api_error",
    "503": "overloaded",
    "529": "overloaded",
}

# The ``"type": "<x>_error"`` form Claude sometimes embeds in the failure text.
# ``authentication_error`` is intentionally absent (handled by auth_patterns.py).
_TYPE_KINDS: dict[str, str] = {
    "invalid_request_error": "invalid_request",
    "permission_error": "permission",
    "not_found_error": "not_found",
    "request_too_large": "request_too_large",
    "rate_limit_error": "rate_limit",
    "api_error": "api_error",
    "overloaded_error": "overloaded",
}

# Kinds that mean the model provider's servers failed, not our request -- these
# earn the "not Minds' fault" note on the frontend.
_PROVIDER_FAULT_KINDS: frozenset[str] = frozenset({"api_error", "overloaded"})

# ``API Error: <code> ...`` -- the surface form Claude Code writes for a failed request.
_API_ERROR_STATUS_RE = re.compile(r"API Error:\s*(\d{3})\b", re.IGNORECASE)
# ``"type": "<x>_error"`` -- the embedded raw-error form.
_API_ERROR_TYPE_RE = re.compile(r'"type"\s*:\s*"([a-z_]+error)"', re.IGNORECASE)


def classify_api_error(text: str) -> str | None:
    """Return a normalized API-error kind for ``text``, or ``None`` when it is not
    a recognized model API error.

    Auth errors return ``None`` here on purpose (see the module docstring): 401 is
    not in the status table and ``authentication_error`` is not in the type table.
    """
    if not text:
        return None
    status_match = _API_ERROR_STATUS_RE.search(text)
    if status_match is not None:
        status_kind = _STATUS_KINDS.get(status_match.group(1))
        if status_kind is not None:
            return status_kind
    type_match = _API_ERROR_TYPE_RE.search(text)
    if type_match is not None:
        type_kind = _TYPE_KINDS.get(type_match.group(1).lower())
        if type_kind is not None:
            return type_kind
    return None


def is_provider_fault(kind: str | None) -> bool:
    """True when ``kind`` is a model-provider-side failure (a 5xx / overloaded)
    rather than a client-side one -- the ones that earn the "not Minds' fault"
    note."""
    return kind in _PROVIDER_FAULT_KINDS
