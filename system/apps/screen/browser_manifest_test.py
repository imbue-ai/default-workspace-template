import json
from pathlib import Path

from screen.browser_manifest import BrowserManifest
from screen.browser_manifest import MANIFEST_VERSION
from screen.browser_manifest import ManifestEntry
from screen.browser_manifest import ManifestStore
from screen.primitives import BrowserName


def _store(tmp_path: Path) -> ManifestStore:
    return ManifestStore(path=tmp_path / "apps" / "browser" / "instances.json")


def test_the_manifest_round_trips_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    written = BrowserManifest(
        browsers=(
            ManifestEntry(id=BrowserName("browser-1"), tabs=("https://www.google.com",), active_tab=0),
            ManifestEntry(id=BrowserName("browser-2"), tabs=("https://a", "https://b"), active_tab=1, stopped=True),
        )
    )
    store.write(written)
    loaded = store.read()
    assert loaded is not None
    assert loaded.version == MANIFEST_VERSION
    assert [entry.id for entry in loaded.browsers] == ["browser-1", "browser-2"]
    assert loaded.browsers[1].tabs == ("https://a", "https://b")
    assert loaded.browsers[1].active_tab == 1
    assert loaded.browsers[1].stopped is True
    assert not store.path.with_suffix(store.path.suffix + ".tmp").exists()


def test_a_write_fully_replaces_the_previous_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(BrowserManifest(browsers=(ManifestEntry(id=BrowserName("browser-1")),)))
    store.write(BrowserManifest(browsers=()))
    loaded = store.read()
    assert loaded is not None and loaded.browsers == ()


def test_a_missing_corrupt_or_old_manifest_reads_as_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read() is None
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{ not valid json")
    assert store.read() is None
    store.path.write_text(json.dumps({"version": 1, "next_id": 3, "browsers": [{"id": 0, "tabs": []}]}))
    assert store.read() is None


def test_fields_an_older_build_wrote_are_ignored_and_the_stopped_flag_defaults_off(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 2,
                "browsers": [{"id": "browser-2", "tabs": ["https://x"], "active_tab": 0, "window_seen": True}],
            }
        )
    )
    loaded = store.read()
    assert loaded is not None
    assert loaded.browsers[0].stopped is False
    assert loaded.browsers[0].tabs == ("https://x",)
