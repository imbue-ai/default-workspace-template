"""Test doubles for the Getting Started app: a catalog fetcher answering from a table, catalog documents, and a fake
shell for the first-visit opener."""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from app_manifest.primitives import AppName
from getting_started.first_window import ShellOpsInterface
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


class FakeShellOps(ShellOpsInterface):
    """A shell whose connected clients, first desktop, and op answers a test sets, recording every op."""

    client_ids: list[str] = Field(default_factory=list, description="The connected clients the shell lists")
    desktop_id: str | None = Field(default="home", description="The first desktop's id, or None for no desktops")
    is_open_refused: bool = Field(default=False, description="Whether the open op is refused")
    is_place_refused: bool = Field(default=False, description="Whether the place op is refused")
    opened: list[tuple[str, str, str, str]] = Field(
        default_factory=list, description="Every open as (app, path, client, desktop)"
    )
    placed: list[tuple[str, str, str, str]] = Field(
        default_factory=list, description="Every place as (window, frame, client, desktop)"
    )

    def connected_client_ids(self) -> list[str]:
        return list(self.client_ids)

    def first_desktop_id(self) -> str | None:
        return self.desktop_id

    def open_window(self, app: AppName, path: str, client_id: str, desktop_id: str) -> str | None:
        self.opened.append((str(app), path, client_id, desktop_id))
        return None if self.is_open_refused else "win-0123456789abcdef"

    def place_window(self, window_id: str, frame: str, client_id: str, desktop_id: str) -> bool:
        self.placed.append((window_id, frame, client_id, desktop_id))
        return not self.is_place_refused
