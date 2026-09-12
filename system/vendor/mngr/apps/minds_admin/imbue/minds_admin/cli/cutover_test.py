import signal

import click
import pytest
from click.testing import CliRunner

from imbue.minds_admin.cli.cutover import confirmation_tier_for_env_name
from imbue.minds_admin.cli.cutover import cutover
from imbue.minds_admin.cli.cutover import immediate_sigint_termination
from imbue.minds_admin.cli.cutover import require_tier_confirmation
from imbue.minds_admin.cli.root import cli


def test_cutover_group_is_registered_with_its_four_commands() -> None:
    result = CliRunner().invoke(cli, ["cutover", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("preflight", "migrate", "rollback", "repave"):
        assert command in result.output


@pytest.mark.parametrize("command", ["migrate", "rollback", "repave"])
def test_mutating_commands_expose_the_tier_flags(command: str) -> None:
    result = CliRunner().invoke(cutover, [command, "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--yes-i-mean-production", "--yes-i-mean-staging", "--yes-i-mean-dev"):
        assert flag in result.output


@pytest.mark.parametrize("command", ["migrate", "repave"])
def test_migrate_and_repave_expose_dry_run(command: str) -> None:
    result = CliRunner().invoke(cutover, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output


def test_migrate_exposes_its_selectors_and_target() -> None:
    result = CliRunner().invoke(cutover, ["migrate", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--target-server-id", "--workspace", "--user", "--source-server-id", "--keep-origin-vm"):
        assert flag in result.output


def test_migrate_requires_at_least_one_selector() -> None:
    result = CliRunner().invoke(cutover, ["migrate", "--target-server-id", "abc", "--yes-i-mean-dev"])
    assert result.exit_code == 2
    assert "select workspaces with" in result.output


def test_preflight_has_no_tier_flag() -> None:
    result = CliRunner().invoke(cutover, ["preflight", "--help"])
    assert result.exit_code == 0, result.output
    assert "--yes-i-mean" not in result.output
    assert "--json-out" in result.output


def test_require_tier_confirmation_accepts_only_the_activated_tiers_flag() -> None:
    require_tier_confirmation(
        "production", is_production_confirmed=True, is_staging_confirmed=False, is_dev_confirmed=False
    )
    with pytest.raises(click.ClickException, match="--yes-i-mean-production"):
        require_tier_confirmation(
            "production", is_production_confirmed=False, is_staging_confirmed=True, is_dev_confirmed=True
        )
    require_tier_confirmation(
        "staging", is_production_confirmed=False, is_staging_confirmed=True, is_dev_confirmed=False
    )
    with pytest.raises(click.ClickException, match="--yes-i-mean-dev"):
        require_tier_confirmation(
            "dev-josh-1", is_production_confirmed=True, is_staging_confirmed=True, is_dev_confirmed=False
        )
    require_tier_confirmation(
        "dev-josh-1", is_production_confirmed=False, is_staging_confirmed=False, is_dev_confirmed=True
    )
    # A ci env has no flag of its own: the dev flag guards it, and the refusal names that flag.
    assert confirmation_tier_for_env_name("ci-abc123") == "dev"
    with pytest.raises(click.ClickException, match="--yes-i-mean-dev"):
        require_tier_confirmation(
            "ci-abc123", is_production_confirmed=False, is_staging_confirmed=False, is_dev_confirmed=False
        )
    require_tier_confirmation(
        "ci-abc123", is_production_confirmed=False, is_staging_confirmed=False, is_dev_confirmed=True
    )


def test_immediate_sigint_termination_uses_the_default_action_and_restores_the_handler() -> None:
    def custom_handler(_signum: int, _frame: object) -> None:
        pass

    previous = signal.signal(signal.SIGINT, custom_handler)
    try:
        with immediate_sigint_termination():
            assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
        assert signal.getsignal(signal.SIGINT) is custom_handler
    finally:
        signal.signal(signal.SIGINT, previous)
