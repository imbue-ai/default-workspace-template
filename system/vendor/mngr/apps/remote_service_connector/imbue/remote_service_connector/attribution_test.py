"""Tests for the marketing-attribution helpers (cookie parsing, merging, context derivation)."""

import re

from imbue.remote_service_connector.attribution import AttributionCookie
from imbue.remote_service_connector.attribution import derive_signup_context
from imbue.remote_service_connector.attribution import parse_attribution_cookie
from imbue.remote_service_connector.attribution import resolve_attribution
from imbue.remote_service_connector.attribution import sanitize_touch
from imbue.remote_service_connector.attribution import synthesize_touch_from_page
from imbue.remote_service_connector.testing import encode_attribution_cookie


def test_parse_attribution_cookie_reads_visitor_id_and_both_touches() -> None:
    cookie_value = encode_attribution_cookie(
        {
            "v": 1,
            "id": "visitor-abc123",
            "first": {"utm_source": "x", "utm_campaign": "launch", "ref": "https://x.com/", "path": "/", "at": "t1"},
            "last": {"utm_source": "newsletter", "q": "utm_source=newsletter", "at": "t2"},
        }
    )

    cookie = parse_attribution_cookie(cookie_value)

    assert cookie is not None
    assert cookie.visitor_id == "visitor-abc123"
    assert cookie.first_touch == {
        "utm_source": "x",
        "utm_campaign": "launch",
        "ref": "https://x.com/",
        "path": "/",
        "at": "t1",
    }
    assert cookie.last_touch == {"utm_source": "newsletter", "q": "utm_source=newsletter", "at": "t2"}


def test_parse_attribution_cookie_tolerates_missing_and_partial_fields() -> None:
    minimal = parse_attribution_cookie(encode_attribution_cookie({"v": 1}))
    assert minimal is not None
    assert minimal.visitor_id is None
    assert minimal.first_touch is None
    assert minimal.last_touch is None

    only_first = parse_attribution_cookie(encode_attribution_cookie({"v": 1, "first": {"gclid": "g-1"}}))
    assert only_first is not None
    assert only_first.first_touch == {"gclid": "g-1"}
    assert only_first.last_touch is None


def test_parse_attribution_cookie_rejects_malformed_values() -> None:
    assert parse_attribution_cookie(None) is None
    assert parse_attribution_cookie("") is None
    assert parse_attribution_cookie("not-json") is None
    assert parse_attribution_cookie(encode_attribution_cookie(["not", "an", "object"])) is None
    assert parse_attribution_cookie(encode_attribution_cookie({"v": 99, "id": "x"})) is None
    assert parse_attribution_cookie(encode_attribution_cookie({"id": "no-version"})) is None
    assert parse_attribution_cookie("x" * 10_000) is None


def test_sanitize_touch_drops_unknown_fields_and_non_strings_and_clamps_long_values() -> None:
    sanitized = sanitize_touch(
        {
            "utm_source": "ok",
            "unknown_field": "dropped",
            "gclid": 123,
            "ref": "",
            "q": "a" * 5000,
        }
    )

    assert sanitized is not None
    assert set(sanitized) == {"utm_source", "q"}
    assert sanitized["utm_source"] == "ok"
    assert len(sanitized["q"]) == 1024

    assert sanitize_touch({"unknown": "only"}) is None
    assert sanitize_touch("not-a-dict") is None


def test_synthesize_touch_from_page_extracts_campaign_params() -> None:
    touch = synthesize_touch_from_page("utm_source=x&utm_campaign=launch&irrelevant=1", "/signup")

    assert touch is not None
    assert touch["utm_source"] == "x"
    assert touch["utm_campaign"] == "launch"
    assert "irrelevant" not in touch
    assert touch["q"] == "utm_source=x&utm_campaign=launch&irrelevant=1"
    assert touch["path"] == "/signup"
    assert touch["at"]


def test_synthesize_touch_from_page_extracts_src_spot_tag_and_iso_z_timestamp() -> None:
    touch = synthesize_touch_from_page("platform=mac&src=modal-pr-review", "/download")

    assert touch is not None
    assert touch["src"] == "modal-pr-review"
    assert "platform" not in touch
    # Exactly JavaScript's toISOString shape (millisecond precision, Z suffix).
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", touch["at"])


def test_synthesize_touch_from_page_returns_none_without_campaign_params() -> None:
    assert synthesize_touch_from_page("", "/signup") is None
    assert synthesize_touch_from_page("next=%2Fmanage&foo=bar", "/signup") is None
    assert synthesize_touch_from_page("utm_source=", "/signup") is None


def test_resolve_attribution_page_params_overwrite_last_and_keep_cookie_first() -> None:
    cookie = AttributionCookie(
        visitor_id="v-1",
        first_touch={"utm_source": "ads", "at": "t1"},
        last_touch={"utm_source": "newsletter", "at": "t2"},
    )
    page_touch = {"utm_source": "signup-link", "at": "t3"}

    resolved = resolve_attribution(cookie, page_touch)

    assert resolved.visitor_id == "v-1"
    assert resolved.first_touch == {"utm_source": "ads", "at": "t1"}
    assert resolved.last_touch == page_touch


def test_resolve_attribution_cookie_alone_and_page_alone() -> None:
    cookie = AttributionCookie(visitor_id="v-1", first_touch={"utm_source": "ads"}, last_touch={"gclid": "g"})
    cookie_only = resolve_attribution(cookie, None)
    assert cookie_only.first_touch == {"utm_source": "ads"}
    assert cookie_only.last_touch == {"gclid": "g"}

    page_touch = {"utm_source": "signup-link", "at": "t"}
    page_only = resolve_attribution(None, page_touch)
    assert page_only.visitor_id is None
    assert page_only.first_touch == page_touch
    assert page_only.last_touch == page_touch

    nothing = resolve_attribution(None, None)
    assert nothing.visitor_id is None
    assert nothing.first_touch is None
    assert nothing.last_touch is None


def test_resolve_attribution_page_touch_backfills_a_first_less_cookie() -> None:
    cookie = AttributionCookie(visitor_id="v-1", first_touch=None, last_touch=None)
    page_touch = {"utm_source": "signup-link", "at": "t"}

    resolved = resolve_attribution(cookie, page_touch)

    assert resolved.visitor_id == "v-1"
    assert resolved.first_touch == page_touch
    assert resolved.last_touch == page_touch


def test_derive_signup_context_classifies_every_surface() -> None:
    assert derive_signup_context("/accounts/authorize?redirect_uri=x") == "desktop_app"
    assert derive_signup_context("/share/authorize?host=h") == "share_visit"
    assert derive_signup_context("/web") == "web_chrome"
    assert derive_signup_context("/web/overview") == "web_chrome"
    assert derive_signup_context("/webby") == "web"
    assert derive_signup_context("/manage") == "web"
    assert derive_signup_context("") == "web"
