"""Unit tests for ``assert_mngr_argv_valid``.

These pin the validator's own behaviour against the live mngr CLI: it must
accept the real invocations the repo emits and reject the kinds of drift a
vendor/mngr CLI change introduces (removed subcommand, removed/renamed flag,
bogus flag) -- which is the exact regression that slipped through on PR 77.
"""

from __future__ import annotations

import pytest

from mngr_cli_contract.contract import MngrArgvContractError, assert_mngr_argv_valid


@pytest.mark.parametrize(
    "argv",
    [
        # The real create/message/rsync/observe invocations the repo emits.
        ["mngr", "create", "demo", "-t", "worker", "--label", "workspace=ws"],
        ["mngr", "message", "demo", "--message-file", "/tmp/does-not-exist.md"],
        ["mngr", "message", "demo", "-m", "hello"],
        ["mngr", "rsync", "/x/", "demo:/x/", "--uncommitted-changes=merge"],
        ["mngr", "observe", "--discovery-only", "--events-dir", "/tmp/e"],
        # A non-"mngr" binary path in argv[0] is ignored (only argv[1:] matters).
        ["/path/to/custom-mngr", "message", "demo", "-m", "hi"],
    ],
)
def test_accepts_real_invocations(argv: list[str]) -> None:
    assert_mngr_argv_valid(argv)


def test_rejects_removed_subcommand() -> None:
    """The literal PR 77 regression: ``push`` was removed in favour of ``rsync``."""
    with pytest.raises(MngrArgvContractError, match="not accepted"):
        assert_mngr_argv_valid(
            ["mngr", "push", "demo:/x/", "--source", "/x/", "--uncommitted-changes=merge"]
        )


def test_rejects_removed_flag_on_existing_subcommand() -> None:
    """``rsync`` exists but takes positional SOURCE DEST, not ``--source`` -- a
    naive push->rsync rename that kept the flag would still be broken, and a
    subcommand-only check would miss it."""
    with pytest.raises(MngrArgvContractError):
        assert_mngr_argv_valid(["mngr", "rsync", "demo:/x/", "--source", "/x/"])


def test_rejects_bogus_flag() -> None:
    with pytest.raises(MngrArgvContractError):
        assert_mngr_argv_valid(["mngr", "create", "demo", "--no-such-flag"])
