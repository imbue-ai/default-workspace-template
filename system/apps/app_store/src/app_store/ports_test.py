import socket
from pathlib import Path

import pytest

from app_store.errors import PortError
from app_store.ports import HIGHEST_AUTO_PORT
from app_store.ports import LOWEST_AUTO_PORT
from app_store.ports import is_port_bound
from app_store.ports import pick_free_port
from app_store.ports import ports_in_registry
from app_store.ports import ports_named_in


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    conf = tmp_path / "system" / "supervisord.conf"
    (tmp_path / "system" / "supervisord.conf.d").mkdir(parents=True)
    conf.write_text("command=forward_port.py --url http://localhost:8080\n")
    (tmp_path / "system" / "supervisord.conf.d" / "a.conf").write_text("--url http://127.0.0.1:8081 && exec a\n")
    apps_toml = tmp_path / "apps.toml"
    apps_toml.write_text('[[apps]]\nname = "x"\nurl = "http://localhost:8082"\n')
    return conf, apps_toml


def test_the_lowest_port_nothing_names_holds_or_listens_on_is_picked(tmp_path: Path) -> None:
    conf, apps_toml = _workspace(tmp_path)
    # 8080-8082 are named, 8083 is reserved, 8084 is held by another install, 8085 is bound.
    assert pick_free_port(conf, apps_toml, held_by_store=[8084], is_bound=lambda port: port == 8085) == 8086


def test_no_free_port_in_the_range_raises(tmp_path: Path) -> None:
    conf, apps_toml = _workspace(tmp_path)
    with pytest.raises(PortError, match=f"{LOWEST_AUTO_PORT} and {HIGHEST_AUTO_PORT}"):
        pick_free_port(conf, apps_toml, held_by_store=[], is_bound=lambda port: True)


def test_ports_are_read_from_urls_and_a_bad_registry_names_none(tmp_path: Path) -> None:
    assert ports_named_in("http://localhost:1 https://127.0.0.1:2 http://example.com:3") == {1, 2}
    assert ports_in_registry(tmp_path / "missing.toml") == set()
    broken = tmp_path / "broken.toml"
    broken.write_text("[[apps]\n")
    assert ports_in_registry(broken) == set()


def test_a_listening_port_reads_as_bound() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert is_port_bound(listener.getsockname()[1])
