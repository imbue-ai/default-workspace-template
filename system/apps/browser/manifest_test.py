import json

from browser import manifest

# The autouse conftest fixture points manifest._MANIFEST_PATH at a fresh per-test tmp.


def test_manifest_roundtrip() -> None:
    written = manifest.Manifest(
        browsers=[
            manifest.ManifestEntry(id="alex-smith", tabs=["https://www.google.com"], active_tab=0),
            manifest.ManifestEntry(id="riley-jones", tabs=["https://a", "https://b"], active_tab=1),
        ],
    )
    manifest.write_manifest(written)
    loaded = manifest.read_manifest()
    assert loaded is not None
    assert loaded.version == manifest._MANIFEST_VERSION
    assert [e.id for e in loaded.browsers] == ["alex-smith", "riley-jones"]
    assert loaded.browsers[1].tabs == ["https://a", "https://b"]
    assert loaded.browsers[1].active_tab == 1
    # The atomic write leaves no temp file behind.
    path = manifest.manifest_path()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_manifest_has_no_next_id() -> None:
    # Ids are random names now; there is no monotonic id high-water mark to persist.
    fields = set(manifest.Manifest().model_dump().keys())
    assert "next_id" not in fields
    assert fields == {"version", "browsers"}


def test_write_fully_replaces_previous() -> None:
    manifest.write_manifest(manifest.Manifest(browsers=[manifest.ManifestEntry(id="alex-smith")]))
    manifest.write_manifest(manifest.Manifest(browsers=[]))
    loaded = manifest.read_manifest()
    assert loaded is not None and loaded.browsers == []


def test_read_missing_returns_none() -> None:
    # Nothing written yet in this test's fresh tmp dir.
    assert manifest.read_manifest() is None


def test_read_corrupt_returns_none() -> None:
    path = manifest.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    assert manifest.read_manifest() is None
    # Valid JSON but wrong schema (extra="forbid") is also treated as missing, not a crash.
    path.write_text('{"version": 2, "totally": "bogus"}')
    assert manifest.read_manifest() is None


def test_read_falls_back_to_the_legacy_path_until_the_new_path_is_written() -> None:
    # A workspace upgraded across the move to data/.apps/browser/instances.json has its
    # saved fleet only at the old path: reads must find it there, and the first write
    # to the new path then takes precedence over whatever the old file still says.
    legacy = manifest._LEGACY_MANIFEST_PATH
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(manifest.Manifest(browsers=[manifest.ManifestEntry(id="browser-1")]).model_dump_json())
    loaded = manifest.read_manifest()
    assert loaded is not None
    assert [e.id for e in loaded.browsers] == ["browser-1"]
    manifest.write_manifest(manifest.Manifest(browsers=[manifest.ManifestEntry(id="browser-2")]))
    reloaded = manifest.read_manifest()
    assert reloaded is not None
    assert [e.id for e in reloaded.browsers] == ["browser-2"]
    assert legacy.exists()


def test_read_rejects_old_version_manifest() -> None:
    # A pre-name v1 manifest (int ids + next_id) must be IGNORED, not silently coerced
    # into string ids -- the version gate is what makes the int->name upgrade a clean
    # break (the fleet then re-scans profiles, skipping legacy numeric dirs).
    path = manifest.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "next_id": 3, "browsers": [{"id": 0, "tabs": []}]}))
    assert manifest.read_manifest() is None
