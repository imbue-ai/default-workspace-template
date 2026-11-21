"""Tests for the template catalog: the document's parse, the URL resolution, and the store's fetch, cache, and fallback."""

import json
from pathlib import Path
from typing import Any

import pytest

from getting_started.template_catalog import CATALOG_CACHE_FILENAME
from getting_started.template_catalog import TemplateCatalogAvailability
from getting_started.template_catalog import TemplateCatalogFormatError
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.template_catalog import catalog_wire
from getting_started.template_catalog import parse_template_catalog
from getting_started.template_catalog import resolve_thumbnail_url
from getting_started.testing import FakeTemplateCatalogFetcher
from getting_started.testing import catalog_app_document
from getting_started.testing import catalog_document
from getting_started.testing import catalog_template_document

_CATALOG_URL = "https://example.test/catalog/new-tab-templates.json"


def _store(tmp_path: Path, fetcher: FakeTemplateCatalogFetcher, **overrides: Any) -> TemplateCatalogStore:
    return TemplateCatalogStore(
        catalog_url=_CATALOG_URL, cache_path=tmp_path / CATALOG_CACHE_FILENAME, fetcher=fetcher, **overrides
    )


# the parse
def test_parse_reads_a_format_1_document_and_fills_the_optional_fields() -> None:
    catalog = parse_template_catalog(
        catalog_document(
            catalog_template_document(
                "inbox", what_it_is="Many\nlines.", author="kanjun", needs_ai=True, unknown_field="ignored"
            ),
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
        catalog_document(
            catalog_template_document("good"), {"slug": "", "title": "No slug"}, {"title": "No repository url"}
        ),
        _CATALOG_URL,
    )
    assert [template.slug for template in catalog.templates] == ["good"]


def test_parse_reads_the_apps_a_catalog_lists_and_fills_their_optional_fields() -> None:
    catalog = parse_template_catalog(
        catalog_document(
            catalog_template_document("inbox"),
            apps=[
                catalog_app_document(
                    "weather-desk",
                    title="Weather Desk",
                    description="Forecasts for the places you care about.",
                    author="someone",
                    version="1.2.0",
                    required_accounts=[{"scope": "slack-api", "permission": "slack-read-all", "note": "Posts it"}],
                    required_secrets=["OPENWEATHER_API_KEY"],
                    needs_ai=True,
                    apt_packages=["ffmpeg"],
                    unknown_field="ignored",
                ),
                catalog_app_document("plain"),
            ],
            shelves=[{"key": "popular", "title": "Most popular", "slugs": ["app:weather-desk", "inbox"]}],
        ),
        _CATALOG_URL,
    )
    weather, plain = catalog.apps
    assert weather.name == "weather-desk"
    assert weather.title == "Weather Desk"
    assert weather.repository_url == "https://github.com/someone/weather-desk"
    assert weather.version == "1.2.0"
    assert [(account.scope, account.permission) for account in weather.required_accounts] == [
        ("slack-api", "slack-read-all")
    ]
    assert weather.required_secrets == ("OPENWEATHER_API_KEY",)
    assert weather.needs_ai is True
    assert weather.apt_packages == ("ffmpeg",)
    assert (plain.description, plain.author, plain.version, plain.needs_ai) == ("", "", "", False)
    assert plain.required_accounts == () and plain.required_secrets == () and plain.apt_packages == ()
    # A shelf's entries are kept as written: the page resolves them, and drops the ones it cannot.
    assert catalog.shelves[0].slugs == ("app:weather-desk", "inbox")


def test_parse_lists_no_apps_for_a_catalog_written_before_apps_could_be_published() -> None:
    body = catalog_document(catalog_template_document("inbox"))
    assert "apps" not in json.loads(body)
    catalog = parse_template_catalog(body, _CATALOG_URL)
    assert catalog.apps == ()
    assert [template.slug for template in catalog.templates] == ["inbox"]


def test_parse_skips_an_app_that_does_not_validate_and_keeps_the_rest() -> None:
    catalog = parse_template_catalog(
        catalog_document(
            catalog_template_document("inbox"),
            apps=[
                catalog_app_document("good"),
                {"name": "", "title": "No name", "repository_url": "https://github.com/someone/unnamed"},
                {"name": "no-repository", "title": "No repository url"},
                "not an object",
            ],
        ),
        _CATALOG_URL,
    )
    assert [app.name for app in catalog.apps] == ["good"]
    assert [template.slug for template in catalog.templates] == ["inbox"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"format": 2, "templates": []}).encode(),
        json.dumps({"format": 1}).encode(),
        json.dumps({"format": 1, "templates": "nope"}).encode(),
        json.dumps({"format": 1, "templates": [], "apps": "nope"}).encode(),
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


def test_the_catalog_wire_carries_resolved_thumbnail_urls_and_the_shelves() -> None:
    catalog = parse_template_catalog(
        catalog_document(
            catalog_template_document("inbox"),
            shelves=[{"key": "popular", "title": "Most popular", "slugs": ["inbox"]}],
        ),
        _CATALOG_URL,
    )
    wire = catalog_wire(catalog, _CATALOG_URL).model_dump(mode="json")
    assert wire["templates"][0]["thumbnail_url"] == "https://example.test/catalog/thumbnails/someone--inbox.svg"
    assert "thumbnail" not in wire["templates"][0]
    assert wire["shelves"] == [{"key": "popular", "title": "Most popular", "slugs": ["inbox"]}]


def test_the_catalog_wire_carries_the_apps_with_resolved_icon_urls_and_their_shelf_entries() -> None:
    catalog = parse_template_catalog(
        catalog_document(
            catalog_template_document("inbox"),
            apps=[catalog_app_document("weather-desk"), catalog_app_document("bare", icon="")],
            shelves=[{"key": "popular", "title": "Most popular", "slugs": ["inbox", "app:weather-desk"]}],
        ),
        _CATALOG_URL,
    )
    wire = catalog_wire(catalog, _CATALOG_URL).model_dump(mode="json")
    weather, bare = wire["apps"]
    assert weather == {
        "name": "weather-desk",
        "title": "Weather-Desk",
        "description": "",
        "repository_url": "https://github.com/someone/weather-desk",
        "author": "",
        "icon_url": "https://example.test/catalog/app-icons/someone--weather-desk.svg",
        "version": "",
        "required_accounts": [],
        "required_secrets": [],
        "needs_ai": False,
        "apt_packages": [],
    }
    assert bare["icon_url"] == ""
    assert wire["shelves"] == [{"key": "popular", "title": "Most popular", "slugs": ["inbox", "app:weather-desk"]}]


def test_the_catalog_wire_of_a_catalog_with_no_apps_carries_an_empty_list() -> None:
    catalog = parse_template_catalog(catalog_document(catalog_template_document("inbox")), _CATALOG_URL)
    assert catalog_wire(catalog, _CATALOG_URL).model_dump(mode="json")["apps"] == []


# the store
def test_store_is_disabled_without_a_url(tmp_path: Path) -> None:
    store = TemplateCatalogStore(
        catalog_url="", cache_path=tmp_path / CATALOG_CACHE_FILENAME, fetcher=FakeTemplateCatalogFetcher()
    )
    reading = store.read()
    assert reading.availability is TemplateCatalogAvailability.DISABLED
    assert reading.catalog is None


def test_store_fetches_once_within_the_freshness_window_and_caches_to_disk(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher(
        body_by_url={_CATALOG_URL: catalog_document(catalog_template_document("inbox"))}
    )
    store = _store(tmp_path, fetcher)

    first = store.read()
    second = store.read()

    assert first.availability is TemplateCatalogAvailability.FRESH
    assert first.catalog is not None and [template.slug for template in first.catalog.templates] == ["inbox"]
    assert second == first
    assert fetcher.fetched_urls == [_CATALOG_URL]
    cached = json.loads((tmp_path / CATALOG_CACHE_FILENAME).read_text())
    assert [template["slug"] for template in cached["templates"]] == ["inbox"]


def test_store_answers_the_disk_copy_as_stale_when_the_fetch_fails(tmp_path: Path) -> None:
    seeding_fetcher = FakeTemplateCatalogFetcher(
        body_by_url={_CATALOG_URL: catalog_document(catalog_template_document("inbox"))}
    )
    assert _store(tmp_path, seeding_fetcher).read().availability is TemplateCatalogAvailability.FRESH

    # A new process: nothing in memory, the URL unreachable, the disk copy present.
    store = _store(tmp_path, FakeTemplateCatalogFetcher())
    reading = store.read()

    assert reading.availability is TemplateCatalogAvailability.STALE
    assert reading.catalog is not None and [template.slug for template in reading.catalog.templates] == ["inbox"]


def test_store_keeps_the_apps_in_its_disk_copy_and_reads_a_copy_written_before_apps(tmp_path: Path) -> None:
    seeding_fetcher = FakeTemplateCatalogFetcher(
        body_by_url={
            _CATALOG_URL: catalog_document(
                catalog_template_document("inbox"), apps=[catalog_app_document("weather-desk")]
            )
        }
    )
    assert _store(tmp_path, seeding_fetcher).read().availability is TemplateCatalogAvailability.FRESH

    # A new process with the URL unreachable answers the apps from the disk copy.
    reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert reading.availability is TemplateCatalogAvailability.STALE
    assert reading.catalog is not None and [app.name for app in reading.catalog.apps] == ["weather-desk"]

    # A copy an older version of the app wrote has no apps key; it still reads, with no apps.
    cache_path = tmp_path / CATALOG_CACHE_FILENAME
    older_copy = json.loads(cache_path.read_text())
    del older_copy["apps"]
    cache_path.write_text(json.dumps(older_copy))
    older_reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert older_reading.catalog is not None and older_reading.catalog.apps == ()
    assert [template.slug for template in older_reading.catalog.templates] == ["inbox"]


def test_store_is_unavailable_with_no_fetch_and_no_disk_copy(tmp_path: Path) -> None:
    reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert reading.availability is TemplateCatalogAvailability.UNAVAILABLE
    assert reading.catalog is None


def test_store_waits_out_the_retry_window_after_a_failure_then_fetches_again(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher()
    store = _store(tmp_path, fetcher, retry_after_failure_seconds=3600.0)

    assert store.read().availability is TemplateCatalogAvailability.UNAVAILABLE
    fetcher.body_by_url[_CATALOG_URL] = catalog_document(catalog_template_document("inbox"))
    assert store.read().availability is TemplateCatalogAvailability.UNAVAILABLE
    assert fetcher.fetched_urls == [_CATALOG_URL]

    # With the window already past, the read after a failure fetches again and picks the catalog up.
    eager_fetcher = FakeTemplateCatalogFetcher()
    eager = _store(tmp_path, eager_fetcher, retry_after_failure_seconds=0.0)
    assert eager.read().availability is TemplateCatalogAvailability.UNAVAILABLE
    eager_fetcher.body_by_url[_CATALOG_URL] = catalog_document(catalog_template_document("inbox"))
    assert eager.read().availability is TemplateCatalogAvailability.FRESH
    assert eager_fetcher.fetched_urls == [_CATALOG_URL, _CATALOG_URL]


def test_store_refetches_once_the_copy_is_no_longer_fresh_and_keeps_the_old_one_on_a_refusal(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher(
        body_by_url={_CATALOG_URL: catalog_document(catalog_template_document("inbox"))}
    )
    store = _store(tmp_path, fetcher, fresh_for_seconds=0.0, retry_after_failure_seconds=0.0)

    assert store.read().availability is TemplateCatalogAvailability.FRESH
    fetcher.body_by_url[_CATALOG_URL] = b"not a catalog any more"
    reading = store.read()

    assert fetcher.fetched_urls == [_CATALOG_URL, _CATALOG_URL]
    assert reading.availability is TemplateCatalogAvailability.STALE
    assert reading.catalog is not None and [template.slug for template in reading.catalog.templates] == ["inbox"]


def test_store_still_answers_the_fetched_catalog_when_the_cache_cannot_be_written(tmp_path: Path) -> None:
    """A cache that cannot be written costs the next process its fallback, not this one its answer."""
    # The cache path's parent is a regular file, so the directory for the copy cannot be made.
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("")
    fetcher = FakeTemplateCatalogFetcher(
        body_by_url={_CATALOG_URL: catalog_document(catalog_template_document("inbox"))}
    )
    store = TemplateCatalogStore(
        catalog_url=_CATALOG_URL, cache_path=blocking_file / CATALOG_CACHE_FILENAME, fetcher=fetcher
    )

    reading = store.read()

    assert reading.availability is TemplateCatalogAvailability.FRESH
    assert reading.catalog is not None and [template.slug for template in reading.catalog.templates] == ["inbox"]


def test_store_ignores_a_disk_copy_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / CATALOG_CACHE_FILENAME).write_text('{"format": 7}')
    reading = _store(tmp_path, FakeTemplateCatalogFetcher()).read()
    assert reading.availability is TemplateCatalogAvailability.UNAVAILABLE
