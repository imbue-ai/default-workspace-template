from pathlib import Path

import pytest

from workspace_usage.context import (
    USAGE_CONTEXT_ENV_VAR,
    UsageContextError,
    app_name_for_program,
    format_usage_context,
    parse_usage_context,
    read_usage_context,
    read_workspace_id,
)


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "apps.toml"
    path.write_text(
        '[[apps]]\nname = "files"\nurl = "http://localhost:8300"\n\n'
        '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\nprogram = "system_interface"\n\n'
        '[[apps]]\nname = "invoice-sorter"\nurl = "http://localhost:8091"\nprogram = "invoice-sorter-web"\n',
        encoding="utf-8",
    )
    return path


def test_format_and_parse_round_trip_values_with_separators() -> None:
    context = {
        "origin_kind": "ROUTINE",
        "origin_id": "nightly,report=v2",
        "run_id": "r-1",
    }

    serialized = format_usage_context(context)

    assert (
        serialized == "origin_kind=ROUTINE,origin_id=nightly%2Creport%3Dv2,run_id=r-1"
    )
    assert parse_usage_context(serialized) == context


@pytest.mark.parametrize("value", ["origin_id=x", "origin_kind", "origin_kind=APP,x"])
def test_parse_rejects_malformed_contexts(value: str) -> None:
    with pytest.raises(UsageContextError):
        parse_usage_context(value)


def test_parse_reads_a_newer_starters_context() -> None:
    assert parse_usage_context("origin_kind=NEWER_KIND,origin_id=c1,colour=red") == {
        "origin_kind": "UNKNOWN",
        "origin_id": "c1",
    }


def test_explicit_context_wins_over_the_supervisord_program(tmp_path: Path) -> None:
    environ = {
        USAGE_CONTEXT_ENV_VAR: "origin_kind=CHAT,origin_id=c-9",
        "SUPERVISOR_PROCESS_NAME": "files",
    }

    assert read_usage_context(environ, _registry(tmp_path)) == {
        "origin_kind": "CHAT",
        "origin_id": "c-9",
    }


def test_a_supervised_app_is_attributed_to_its_manifest_name(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert read_usage_context({"SUPERVISOR_PROCESS_NAME": "files"}, registry) == {
        "origin_kind": "APP",
        "origin_id": "files",
    }
    assert read_usage_context(
        {"SUPERVISOR_PROCESS_NAME": "invoice-sorter-web"}, registry
    ) == {
        "origin_kind": "APP",
        "origin_id": "invoice-sorter",
    }


def test_a_supervised_program_that_is_not_an_app_is_attributed_to_the_system(
    tmp_path: Path,
) -> None:
    assert read_usage_context(
        {"SUPERVISOR_PROCESS_NAME": "host-backup"}, _registry(tmp_path)
    ) == {
        "origin_kind": "SYSTEM",
        "origin_id": "host-backup",
    }


def test_a_malformed_context_falls_back_to_the_program(tmp_path: Path) -> None:
    environ = {USAGE_CONTEXT_ENV_VAR: "garbage", "SUPERVISOR_PROCESS_NAME": "files"}

    assert read_usage_context(environ, _registry(tmp_path))["origin_id"] == "files"


def test_an_unsupervised_process_without_context_is_unknown(tmp_path: Path) -> None:
    assert read_usage_context({}, _registry(tmp_path)) == {"origin_kind": "UNKNOWN"}


def test_a_missing_registry_finds_no_app(tmp_path: Path) -> None:
    assert app_name_for_program("files", tmp_path / "absent.toml") is None


def test_workspace_id_is_the_services_agent_id() -> None:
    assert read_workspace_id({"MNGR_AGENT_ID": "agent-3f2a"}) == "agent-3f2a"
    assert read_workspace_id({}) != ""
