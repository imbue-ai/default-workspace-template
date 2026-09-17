from click.testing import CliRunner

from imbue.minds_admin.cli.workspaces_admin import workspaces_admin

_HOST_DB_ID = "11111111-2222-3333-4444-555555555555"


def test_stop_requires_a_kind_before_any_connector_call() -> None:
    # An unkinded operator stop would read as idle on the server; the CLI
    # refuses it up front rather than letting the omission pick the kind.
    result = CliRunner().invoke(workspaces_admin, ["stop", _HOST_DB_ID])
    assert result.exit_code == 2
    assert "--kind" in result.output


def test_stop_and_set_stop_kind_accept_only_the_operator_kinds() -> None:
    # The choice set is derived from WorkspaceStopKind: the owner's own kind and
    # the client-side coercion target must not be an operator's to stamp.
    for refused_kind in ("owner", "unknown"):
        stop = CliRunner().invoke(workspaces_admin, ["stop", _HOST_DB_ID, "--kind", refused_kind])
        assert stop.exit_code == 2, stop.output
        assert "Invalid value" in stop.output
        set_kind = CliRunner().invoke(workspaces_admin, ["set-stop-kind", _HOST_DB_ID, refused_kind])
        assert set_kind.exit_code == 2, set_kind.output
        assert "Invalid value" in set_kind.output
    for args in (["stop", "--help"], ["set-stop-kind", "--help"]):
        help_output = CliRunner().invoke(workspaces_admin, args).output
        for kind in ("maintenance", "idle", "suspension"):
            assert kind in help_output
