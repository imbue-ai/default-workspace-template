"""Unit tests for ``assert_mngr_argv_valid``.

These pin the validator's own behaviour against the live mngr CLI: it must
accept the real invocations the repo emits and reject the kinds of drift a
system/vendor/mngr CLI change introduces -- a removed subcommand, a removed or renamed
flag, or a bogus flag.
"""

from __future__ import annotations

import pytest

from mngr_cli_contract.contract import (
    MngrArgvContractError,
    MngrSettingContractError,
    assert_mngr_argv_valid,
)


@pytest.mark.parametrize(
    "argv",
    [
        # The real create/message/rsync/observe invocations the repo emits.
        ["mngr", "create", "demo", "-t", "worker", "--label", "workspace=ws"],
        ["mngr", "message", "demo", "--message-file", "/tmp/does-not-exist.md"],
        ["mngr", "message", "demo", "-m", "hello"],
        ["mngr", "rsync", "/x/", "demo:/x/", "--uncommitted-changes=merge"],
        ["mngr", "observe", "--discovery-only", "--events-dir", "/tmp/e"],
        # The chat-create fast-mode override, in both -S spellings.
        ["mngr", "create", "demo", "-S", "agent_types.claude.settings_overrides.fastMode=false"],
        ["mngr", "create", "demo", "--setting=agent_types.claude.settings_overrides.fastMode=true"],
        # A non-"mngr" binary path in argv[0] is ignored (only argv[1:] matters).
        ["/path/to/custom-mngr", "message", "demo", "-m", "hi"],
    ],
)
def test_accepts_real_invocations(argv: list[str]) -> None:
    assert_mngr_argv_valid(argv)


def test_rejects_removed_subcommand() -> None:
    """A subcommand the live CLI does not have is rejected (``push`` is a
    genuinely removed command -- mngr replaced it with ``rsync``)."""
    with pytest.raises(MngrArgvContractError, match="not accepted"):
        assert_mngr_argv_valid(
            ["mngr", "push", "demo:/x/", "--source", "/x/", "--uncommitted-changes=merge"]
        )


def test_rejects_removed_flag_on_existing_subcommand() -> None:
    """``rsync`` exists but takes positional ``SOURCE DEST``, not ``--source``,
    so this exercises a removed/renamed flag on an existing subcommand -- a case
    a subcommand-only check would miss."""
    with pytest.raises(MngrArgvContractError):
        assert_mngr_argv_valid(["mngr", "rsync", "demo:/x/", "--source", "/x/"])


def test_rejects_bogus_flag() -> None:
    with pytest.raises(MngrArgvContractError):
        assert_mngr_argv_valid(["mngr", "create", "demo", "--no-such-flag"])


@pytest.mark.parametrize(
    "setting",
    [
        # A field the owning section does not have.
        "agent_types.claude.no_such_field=1",
        # A settings_overrides leaf on a custom agent type. mngr parses a -S as
        # its own config layer, so the type is resolved without the settings
        # file's parent_type and validated against the base agent config, which
        # has no settings_overrides field. This is exactly why the repo's
        # chat-create paths target `claude` instead of `chat`.
        "agent_types.chat.settings_overrides.fastMode=false",
        # A section that does not exist at all.
        "no_such_section.key=1",
    ],
)
def test_rejects_setting_that_does_not_resolve(setting: str) -> None:
    """click treats a ``-S`` value as an opaque string, so an unresolvable key
    path reaches mngr and takes the whole command down at runtime. It must fail
    here instead."""
    with pytest.raises(MngrSettingContractError, match="not accepted"):
        assert_mngr_argv_valid(["mngr", "create", "demo", "-S", setting])
