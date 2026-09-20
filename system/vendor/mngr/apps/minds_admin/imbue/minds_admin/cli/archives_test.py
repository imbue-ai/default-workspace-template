from click.testing import CliRunner

from imbue.minds_admin.cli.archives import archives
from imbue.minds_admin.cli.root import cli


def test_archives_group_is_registered_with_its_commands() -> None:
    result = CliRunner().invoke(cli, ["archives", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("candidates", "create", "list", "links", "download"):
        assert command in result.output


def test_create_requires_the_tier_flag_and_exposes_its_knobs() -> None:
    help_output = CliRunner().invoke(archives, ["create", "--help"]).output
    for flag in (
        "--workspace",
        "--start-stopped",
        "--owner-email",
        "--skip-owner-lookup",
        "--volume-path",
        "--exclude",
    ):
        assert flag in help_output
    for flag in ("--yes-i-mean-production", "--yes-i-mean-staging", "--yes-i-mean-dev"):
        assert flag in help_output
    # Refused before any env, Vault or box is touched: --workspace is required.
    result = CliRunner().invoke(archives, ["create", "--yes-i-mean-dev"])
    assert result.exit_code == 2
    assert "--workspace" in result.output


def test_links_caps_the_expiry_at_seven_days() -> None:
    result = CliRunner().invoke(archives, ["links", "--expires-days", "8"])
    assert result.exit_code == 2
    assert "8" in result.output and "7" in result.output


def test_read_only_commands_have_no_tier_flag() -> None:
    for command in ("candidates", "list", "links", "download"):
        help_output = CliRunner().invoke(archives, [command, "--help"]).output
        assert "--yes-i-mean" not in help_output, command
