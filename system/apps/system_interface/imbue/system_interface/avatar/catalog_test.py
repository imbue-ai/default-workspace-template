"""Tests for the registered designs' catalog under the app data directory."""

import json
from pathlib import Path

import pytest

from imbue.system_interface.avatar.catalog import AvatarCatalogStore
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.catalog import MAX_REGISTERED_DESIGNS
from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.errors import InvalidShellValueError

_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle r="9"/></svg>'


def _registration(design_id: str, label: str = "Mine") -> DesignRegistration:
    return DesignRegistration(id=DesignId(design_id), label=label, svg=_SVG, source_path=f"/tmp/{design_id}.svg")


def test_the_catalog_lists_the_bundled_designs_then_the_registered_ones(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    assert [listing.id for listing in store.entries()] == [design.id for design in BUNDLED_DESIGNS]
    store.register(_registration("mine"))
    listings = store.entries()
    assert listings[-1].id == "mine"
    assert listings[-1].source_path == "/tmp/mine.svg"
    assert listings[0].source_path is None
    assert store.source("mine") == _SVG
    assert store.source(DEFAULT_DESIGN_ID) is not None
    assert store.source("nobody") is None


def test_registering_an_id_again_replaces_it_in_place(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    store.register(_registration("mine", "First"))
    store.register(_registration("other"))
    store.register(_registration("mine", "Second"))
    labels = [listing.label for listing in store.entries() if listing.source_path is not None]
    assert labels == ["Mine", "Second"]


def test_a_bundled_id_is_never_replaced(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    with pytest.raises(InvalidShellValueError, match="bundled"):
        store.register(_registration(str(DEFAULT_DESIGN_ID)))


def test_the_catalog_never_grows_past_its_bound(tmp_path: Path) -> None:
    store = AvatarCatalogStore(directory=tmp_path / "avatars")
    for index in range(MAX_REGISTERED_DESIGNS):
        store.register(_registration(f"design-{index}"))
    with pytest.raises(InvalidShellValueError, match="replace one"):
        store.register(_registration("one-too-many"))
    store.register(_registration("design-0", "Replaced"))


def test_an_invalid_catalog_reads_as_empty_but_is_not_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "avatars"
    directory.mkdir()
    (directory / "catalog.json").write_text(json.dumps({"version": 1, "designs": [{"id": "x"}]}))
    store = AvatarCatalogStore(directory=directory)
    assert store.read().designs == ()
    with pytest.raises(InvalidShellValueError, match="invalid"):
        store.register(_registration("mine"))


def test_a_registration_carries_a_valid_design() -> None:
    with pytest.raises(ValueError, match="unsupported design SVG element"):
        DesignRegistration(
            id=DesignId("mine"),
            label="Mine",
            svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><script/></svg>',
            source_path="/tmp/mine.svg",
        )
