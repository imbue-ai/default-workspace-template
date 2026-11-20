from pathlib import Path

import pytest

from app_install.errors import PortUnavailableError
from app_install.ports import allocate_port
from app_install.ports import ports_in_use
from app_install.records import AppOrigin
from app_install.records import AppRecord


def _never_bound(port: int) -> bool:
    return False


def _write(repo_root: Path, relative_path: str, text: str) -> None:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _record(name: str, port: int) -> AppRecord:
    return AppRecord(name=name, package=name, port=port, origin=AppOrigin.LOCAL)


def test_an_empty_workspace_gets_the_first_automatic_port(tmp_path: Path) -> None:
    assert allocate_port(tmp_path, (), None, _never_bound) == 8080


def test_a_port_held_by_a_program_a_registry_row_or_a_record_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)
    _write(
        tmp_path,
        "system/supervisord.conf.d/digest.conf",
        '[program:digest]\ncommand=bash -c "forward_port.py --manifest x --url http://localhost:8080 && exec digest"\n',
    )
    _write(tmp_path, "data/.state/apps.toml", '[[apps]]\nname = "notes"\nurl = "http://127.0.0.1:8081"\n')
    records = (_record("inbox", 8082),)

    assert ports_in_use(tmp_path, records) >= {8080, 8081, 8082}
    assert allocate_port(tmp_path, records, None, _never_bound) == 8084


def test_the_ports_the_screen_holds_without_a_program_line_are_never_given_out(tmp_path: Path) -> None:
    records = tuple(
        _record(f"app{port}", port) for port in (8080, 8081, 8082, 8084, 8085, 8086, 8087, 8088, 8089, 8090)
    )

    assert allocate_port(tmp_path, records, None, _never_bound) == 8092


def test_a_port_something_is_bound_to_right_now_is_skipped(tmp_path: Path) -> None:
    assert allocate_port(tmp_path, (), None, lambda port: port in (8080, 8081)) == 8082


def test_a_requested_port_that_is_free_is_the_one_given(tmp_path: Path) -> None:
    assert allocate_port(tmp_path, (_record("inbox", 8080),), 8093, _never_bound) == 8093


@pytest.mark.parametrize("requested_port", [8080, 8083, 8095])
def test_a_requested_port_that_is_taken_is_refused(tmp_path: Path, requested_port: int) -> None:
    records = (_record("inbox", 8080),)

    with pytest.raises(PortUnavailableError, match=str(requested_port)):
        allocate_port(tmp_path, records, requested_port, lambda port: port == 8095)


def test_a_registry_that_does_not_parse_names_no_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)
    _write(tmp_path, "data/.state/apps.toml", "[[apps\n")

    assert allocate_port(tmp_path, (), None, _never_bound) == 8080
