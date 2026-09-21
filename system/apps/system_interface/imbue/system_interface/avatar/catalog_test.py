"""Tests for the registered designs' catalog under the app data directory."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.system_interface.avatar.catalog import AvatarCatalogStore
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.catalog import MAX_REGISTERED_DESIGNS
from imbue.system_interface.avatar.catalog import _MAX_CATALOG_BYTES
from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.testing import MINIMAL_DESIGN_SVG
from imbue.system_interface.avatar.testing import design_registration
from imbue.system_interface.shell.errors import InvalidShellValueError


def test_the_catalog_lists_the_bundled_designs_then_the_registered_ones(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    assert [listing.id for listing in store.entries()] == [design.id for design in BUNDLED_DESIGNS]
    store.register(design_registration("mine"))
    listings = store.entries()
    assert listings[-1].id == "mine"
    assert listings[-1].source_path == "/tmp/mine.svg"
    assert listings[0].source_path is None
    assert store.source("mine") == MINIMAL_DESIGN_SVG
    assert store.source(DEFAULT_DESIGN_ID) is not None
    assert store.source("nobody") is None


def test_registering_an_id_again_replaces_it_and_moves_it_last(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    store.register(design_registration("mine", "First"))
    store.register(design_registration("other"))
    store.register(design_registration("mine", "Second"))
    labels = [listing.label for listing in store.entries() if listing.source_path is not None]
    assert labels == ["Mine", "Second"]


def test_a_stored_design_outlives_a_change_to_the_shared_stylesheet(tmp_path: Path) -> None:
    """The catalog is read without re-validating its drawings: a design registered under an earlier sheet still
    lists and answers its source."""
    directory = tmp_path / "avatars"
    directory.mkdir()
    stale = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><style>.jelly-body{x:1}</style></svg>'
    (directory / "catalog.json").write_text(
        json.dumps({"version": 1, "designs": [{"id": "old", "label": "Old", "svg": stale, "source_path": "/x"}]})
    )
    store = AvatarCatalogStore(directory=directory)
    assert [listing.id for listing in store.entries()][-1] == "old"
    assert store.source("old") == stale
    # A registration is still held to today's sheet.
    with pytest.raises(ValidationError, match="custom CSS"):
        DesignRegistration(id=DesignId("new"), label="New", svg=stale, source_path="/y")


def test_a_bundled_id_is_never_replaced(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    with pytest.raises(InvalidShellValueError, match="bundled"):
        store.register(design_registration(str(DEFAULT_DESIGN_ID)))


def test_the_catalog_never_grows_past_its_bound(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    for index in range(MAX_REGISTERED_DESIGNS):
        store.register(design_registration(f"design-{index}"))
    with pytest.raises(InvalidShellValueError, match="replace one"):
        store.register(design_registration("one-too-many"))
    store.register(design_registration("design-0", "Replaced"))


def test_an_oversized_catalog_reads_as_empty_but_is_not_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "avatars"
    directory.mkdir()
    with (directory / "catalog.json").open("wb") as stream:
        stream.truncate(_MAX_CATALOG_BYTES + 1)
    store = AvatarCatalogStore(directory=directory)
    assert [listing.id for listing in store.entries()] == [design.id for design in BUNDLED_DESIGNS]
    with pytest.raises(InvalidShellValueError, match="storage bound"):
        store.register(design_registration("mine"))


def test_an_invalid_catalog_reads_as_empty_but_is_not_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "avatars"
    directory.mkdir()
    (directory / "catalog.json").write_text(json.dumps({"version": 1, "designs": [{"id": "x"}]}))
    store = AvatarCatalogStore(directory=directory)
    assert store.read().designs == ()
    with pytest.raises(InvalidShellValueError, match="invalid"):
        store.register(design_registration("mine"))


def test_a_registration_carries_a_valid_design() -> None:
    with pytest.raises(ValidationError, match="unsupported design SVG element"):
        DesignRegistration(
            id=DesignId("mine"),
            label="Mine",
            svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><script/></svg>',
            source_path="/tmp/mine.svg",
        )
