"""Test doubles for the Getting Started app: a catalog fetcher answering from a table, and catalog documents."""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from getting_started.template_catalog import TemplateCatalogFetcherInterface


class FakeTemplateCatalogFetcher(TemplateCatalogFetcherInterface):
    """Answers each catalog URL from a table (None for one not in it) and records every fetch."""

    body_by_url: dict[str, bytes] = Field(default_factory=dict, description="What each URL answers")
    fetched_urls: list[str] = Field(default_factory=list, description="Every URL fetched, in order")

    def fetch(self, url: str) -> bytes | None:
        self.fetched_urls.append(url)
        return self.body_by_url.get(url)


def catalog_template_document(slug: str, **overrides: Any) -> dict[str, Any]:
    """One template as a catalog lists it: the four required fields and a relative drawing, derived
    from the slug, with ``overrides`` laid over them."""
    document: dict[str, Any] = {
        "slug": slug,
        "title": slug.title(),
        "description": f"What {slug} does.",
        "repository_url": f"https://github.com/someone/{slug}",
        "thumbnail": f"thumbnails/someone--{slug}.svg",
    }
    document.update(overrides)
    return document


def catalog_document(
    *templates: Mapping[str, Any], shelves: Sequence[Mapping[str, Any]] = (), **overrides: Any
) -> bytes:
    """A format-1 catalog document as a fetcher answers it, holding ``templates`` and ``shelves``."""
    document: dict[str, Any] = {
        "format": 1,
        "generated_at": "2026-09-07T00:00:00Z",
        "templates": list(templates),
        "shelves": list(shelves),
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")
