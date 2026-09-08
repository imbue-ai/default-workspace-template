"""Tests for the template catalog: the document's parse, the URL resolution, and the store's fetch, cache, and fallback."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.template_catalog import CATALOG_CACHE_FILENAME
from imbue.system_interface.template_catalog import TemplateCatalogAvailability
from imbue.system_interface.template_catalog import TemplateCatalogFormatError
from imbue.system_interface.template_catalog import TemplateCatalogStore
from imbue.system_interface.template_catalog import catalog_wire_json
from imbue.system_interface.template_catalog import parse_template_catalog
from imbue.system_interface.template_catalog import resolve_thumbnail_url
from imbue.system_interface.testing import FakeTemplateCatalogFetcher

_CATALOG_URL = "https://example.test/catalog/new-tab-templates.json"


def _template(slug: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "slug": slug,
        "title": slug.title(),
        "description": f"What {slug} does.",
        "repository_url": f"https://github.com/someone/{slug}",
        "thumbnail": f"thumbnails/someone--{slug}.svg",
    }
    document.update(overrides)
    return document


def _document(*templates: dict[str, Any], shelves: list[dict[str, Any]] | None = None, **overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "format": 1,
        "generated_at": "2026-09-07T00:00:00Z",
        "templates": list(templates),
        "shelves": shelves if shelves is not None else [],
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def _store(tmp_path: Path, fetcher: FakeTemplateCatalogFetcher, **overrides: Any) -> TemplateCatalogStore:
    return TemplateCatalogStore(
        catalog_url=_CATALOG_URL, cache_path=tmp_path / CATALOG_CACHE_FILENAME, fetcher=fetcher, **overrides
    )


# ---------- the parse ----------


def test_parse_reads_a_format_1_document_and_fills_the_optional_fields() -> None:
    catalog = parse_template_catalog(
        _document(
            _template("inbox", what_it_is="Many\nlines.", author="kanjun", needs_ai=True, unknown_field="ignored"),
            shelves=[{"key": "popular", "title": "Most popular", "slugs": ["inbox"], "extra": 1}],
        ),
        _CATALOG_URL,
    )
    (template,) = catalog.templates
    assert template.slug == "inbox"
    assert template.author == "kanjun"
    assert template.needs_ai is True
    assert template.required_accounts == ()
    assert template.version == ""
    (shelf,) = catalog.shelves
    assert shelf.slugs == ("inbox",)


def test_parse_skips_a_template_that_does_not_validate_and_keeps_the_rest() -> None:
    catalog = parse_template_catalog(
        _document(_template("good"), {"slug": "", "title": "No slug"}, {"title": "No repository url"}),
        _CATALOG_URL,
    )
    assert [template.slug for template in catalog.templates] == ["good"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"format": 2, "templates": []}).encode(),
        json.dumps({"format": 1}).encode(),
        json.dumps({"format": 1, "templates": "nope"}).encode(),
    ],
)
def test_parse_refuses_a_document_that_is_not_a_format_1_catalog(body: bytes) -> None:
    with pytest.raises(TemplateCatalogFormatError):
        parse_template_catalog(body, _CATALOG_URL)


def test_thumbnails_resolve_against_the_catalog_url() -> None:
    assert (
        resolve_thumbnail_url(_CATALOG_URL, "thumbnails/a--b.svg")
        == "https://example.test/catalog/thumbnails/a--b.svg"
    )
    assert resolve_thumbnail_url(_CATALOG_URL, "https://cdn.test/art.svg") == "https://cdn.test/art.svg"
    assert resolve_thumbnail_url(_CATALOG_URL, "") == ""


def test_wire_json_carries_resolved_thumbnail_urls_and_the_shelves() -> None:
    catalog = parse_template_catalog(
        _document(_template("inbox"), shelves=[{"key": "popular", "title": "Most popular", "slugs": ["inbox"]}]),
        _CATALOG_URL,
    )
    wire = catalog_wire_json(catalog, _CATALOG_URL)
    assert wire["templates"][0]["thumbnail_url"] == "https://example.test/catalog/thumbnails/someone--inbox.svg"
    assert "thumbnail" not in wire["templates"][0]
    assert wire["shelves"] == [{"key": "popular", "title": "Most popular", "slugs": ["inbox"]}]


# ---------- the store ----------


def test_store_is_disabled_without_a_url(tmp_path: Path) -> None:
    store = TemplateCatalogStore(
        catalog_url="", cache_path=tmp_path / CATALOG_CACHE_FILENAME, fetcher=FakeTemplateCatalogFetcher()
    )
    reading = store.read()
    assert reading.availability is TemplateCatalogAvailability.DISABLED
    assert reading.catalog is None


def test_store_fetches_once_within_the_freshness_window_and_caches_to_disk(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher(body_by_url={_CATALOG_URL: _document(_template("inbox"))})
    store = _store(tmp_path, fetcher)

    first = store.read()
    second = store.read()

    assert first.availability is TemplateCatalogAvailability.FRESH
    assert first.catalog is not None and [t.slug for t in first.catalog.templates] == ["inbox"]
    assert second == first
    assert fetcher.fetched_urls == [_CATALOG_URL]
    cached = json.loads((tmp_path / CATALOG_CACHE_FILENAME).read_text())
    assert [t["slug"] for t in cached["templates"]] == ["inbox"]


def test_store_answers_the_disk_copy_as_stale_when_the_fetch_fails(tmp_path: Path) -> None:
    seeding_fetcher = FakeTemplateCatalogFetcher(body_by_url={_CATALOG_URL: _document(_template("inbox"))})
    assert _store(tmp_path, seeding_fetcher).read().availability is TemplateCatalogAvailability.FRESH

    # A new process: nothing in memory, the URL unreachable, the disk copy present.
    store = _store(tmp_path, FakeTemplateCatalogFetcher())
    reading = store.read()

    assert reading.availability is TemplateCatalogAvailability.STALE
    assert reading.catalog is not None and [t.slug for t in reading.catalog.templates] == ["inbox"]


def test_store_is_unavailable_with_no_fetch_and_no_disk_copy(tmp_path: Path) -> None:
    reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert reading.availability is TemplateCatalogAvailability.UNAVAILABLE
    assert reading.catalog is None


def test_store_waits_out_the_retry_window_after_a_failure_then_fetches_again(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher()
    store = _store(tmp_path, fetcher, retry_after_failure_seconds=3600.0)

    assert store.read().availability is TemplateCatalogAvailability.UNAVAILABLE
    fetcher.body_by_url[_CATALOG_URL] = _document(_template("inbox"))
    assert store.read().availability is TemplateCatalogAvailability.UNAVAILABLE
    assert fetcher.fetched_urls == [_CATALOG_URL]

    eager = _store(tmp_path, fetcher, retry_after_failure_seconds=0.0)
    eager.read()
    assert eager.read().availability is TemplateCatalogAvailability.FRESH


def test_store_refetches_once_the_copy_is_no_longer_fresh_and_keeps_the_old_one_on_a_refusal(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher(body_by_url={_CATALOG_URL: _document(_template("inbox"))})
    store = _store(tmp_path, fetcher, fresh_for_seconds=0.0, retry_after_failure_seconds=0.0)

    assert store.read().availability is TemplateCatalogAvailability.FRESH
    fetcher.body_by_url[_CATALOG_URL] = b"not a catalog any more"
    reading = store.read()

    assert fetcher.fetched_urls == [_CATALOG_URL, _CATALOG_URL]
    assert reading.availability is TemplateCatalogAvailability.STALE
    assert reading.catalog is not None and [t.slug for t in reading.catalog.templates] == ["inbox"]


def test_store_ignores_a_disk_copy_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / CATALOG_CACHE_FILENAME).write_text('{"format": 7}')
    reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert reading.availability is TemplateCatalogAvailability.UNAVAILABLE
