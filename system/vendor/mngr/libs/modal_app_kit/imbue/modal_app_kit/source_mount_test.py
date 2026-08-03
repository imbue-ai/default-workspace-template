from pathlib import Path

from imbue.modal_app_kit.source_mount import shipped_python_source_ignore


def test_shipped_python_source_ignore_ships_only_plain_python_source() -> None:
    ignore = shipped_python_source_ignore

    shipped = [
        Path("core.py"),
        Path("helpers/format.py"),
        Path("helpers/app.py"),
    ]
    not_shipped = [
        Path("data.json"),
        Path("core.pyc"),
        Path("__pycache__/core.cpython-312.pyc"),
        Path("core_test.py"),
        Path("test_integration.py"),
        Path("conftest.py"),
        Path("nested/conftest.py"),
        Path("testing.py"),
        Path("app.py"),
    ]

    assert [path for path in shipped if ignore(path)] == []
    assert [path for path in not_shipped if not ignore(path)] == []
