"""Pins the snapshot contract against the fixture the Imbue Studio desktop publishes.

The desktop writes the snapshot and this library reads it, across two repos: the desktop
publishes ``workspace_profile_contract.json`` beside its writer, the build vendors it here
through ``system/scripts/fetch_mngr_assets.sh`` (at the mngr commit ``pyproject.toml`` pins),
and this test confronts this library's constants with it -- so a format bump or a key added on
one side fails the other until mirrored.

The vendor directory is fetched by the ``system/`` npm pre-build/pre-test hooks, which CI runs
before pytest; a local run that has never built the frontend may not have it, so an absent
vendor directory skips rather than fails.
"""

import json
from pathlib import Path

import pytest

from workspace_profile.snapshot import (
    CONSUMED_SETTING_KEYS,
    SNAPSHOT_HEADER_KEYS,
    SNAPSHOT_RELATIVE_PATH,
    SUPPORTED_SNAPSHOT_FORMAT,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_VENDOR_ASSETS_DIR = _REPO_ROOT / "system" / "vendor" / "mngr-assets"
_FIXTURE_PATH = (
    _VENDOR_ASSETS_DIR
    / "apps"
    / "minds"
    / "imbue"
    / "minds"
    / "desktop_client"
    / "workspace_profile_contract.json"
)


def test_the_snapshot_contract_matches_the_desktops_fixture() -> None:
    if not _VENDOR_ASSETS_DIR.is_dir():
        pytest.skip(
            "mngr assets are not vendored here; run system/scripts/fetch_mngr_assets.sh first"
        )
    # The directory exists, so the fetch ran: a missing fixture means the pinned mngr commit
    # does not publish it (or the asset list lost it), which is a real contract break.
    contract = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

    assert contract["snapshot_format"] == SUPPORTED_SNAPSHOT_FORMAT
    assert contract["snapshot_relative_path"] == SNAPSHOT_RELATIVE_PATH
    assert set(contract["header_keys"]) == set(SNAPSHOT_HEADER_KEYS)
    assert tuple(contract["setting_keys"]) == CONSUMED_SETTING_KEYS
