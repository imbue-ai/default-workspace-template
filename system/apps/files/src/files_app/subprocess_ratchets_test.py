"""Ratchet keeping this app's subprocess spawning on the detached runner.

The files app spawns nothing itself: dufs is started for it by ``app_instances.sidecar``,
which detaches it. That makes this ratchet a guard rather than a fix -- it holds the count
at zero so a spawn added directly here later cannot quietly reintroduce the exposure the
sidecar already closed. See ``detached_subprocess.runner`` for what that exposure is.

The rules themselves live in ``detached_subprocess.ratchets``; every supervisord program in this
repo applies the same two to its own tree. This file supplies only the source tree to scan.

Named ``*_test.py`` because that is this repo's convention for a unit test, which a static scan
is. (``test_*_ratchets.py`` is the per-project standard set, which ``system/test_meta_ratchets.py``
requires to be exactly one per project defining identical tests.)
"""

from pathlib import Path

import pytest
from detached_subprocess.ratchets import BACKGROUND_SPAWN_RULE, RAW_SPAWN_RULE
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet
from inline_snapshot import snapshot

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)


def test_prevent_background_process_spawns() -> None:
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)
