import sys
from pathlib import Path

import pytest

from share_gateway.runner import (
    POLL_INTERVAL_SECONDS,
    _try_setup_inotify,
    _wait_for_change_inotify,
)


@pytest.mark.skipif(sys.platform != "linux", reason="inotify is a Linux facility")
def test_share_env_write_wakes_the_watcher_before_the_poll_interval(
    tmp_path: Path,
) -> None:
    share_env = tmp_path / "data" / ".secrets" / "share.env"
    inotify = _try_setup_inotify([share_env])
    assert inotify is not None, (
        "inotify_simple is a declared dependency, so the watch must be set up on Linux"
    )

    share_env.write_text("RELAY_TOKEN=x\n")

    # A write must wake the loop at once; a blind poll would sleep the whole interval.
    assert _wait_for_change_inotify(inotify, timeout_seconds=1.0) is True
    assert POLL_INTERVAL_SECONDS > 1.0


@pytest.mark.skipif(sys.platform != "linux", reason="inotify is a Linux facility")
def test_watcher_times_out_quietly_when_nothing_changes(tmp_path: Path) -> None:
    inotify = _try_setup_inotify([tmp_path / "share.env"])
    assert inotify is not None

    assert _wait_for_change_inotify(inotify, timeout_seconds=0.1) is False
