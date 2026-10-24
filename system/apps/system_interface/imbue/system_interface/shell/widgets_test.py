import json
from pathlib import Path

from app_manifest.manifest import Widget
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from workspace_layout.answers import WidgetState

from imbue.system_interface.shell.widgets import WIDGETS_FILENAME
from imbue.system_interface.shell.widgets import WidgetsStore
from imbue.system_interface.shell.widgets import enabled_widget_apps
from imbue.system_interface.shell.widgets import widget_states


def _row(name: str, widget: Widget | None, is_internal: bool = False) -> RegistryRow:
    return RegistryRow.model_validate(
        {
            "name": name,
            "url": "http://localhost:7000",
            "label": f"{name}-abcd1234",
            "internal": is_internal,
            "widget": widget.model_dump() if widget is not None else None,
        }
    )


def test_each_declared_widget_takes_the_stored_choice_else_its_manifest_default() -> None:
    rows = [
        _row("gauge", Widget(path="/widget")),
        _row("files", None),
        _row("meter", Widget(path="/widget", default_enabled=False)),
        _row("probe", Widget(path="/widget"), is_internal=True),
    ]

    assert widget_states(rows, {}) == (
        WidgetState(app=AppName("gauge"), is_enabled=True),
        WidgetState(app=AppName("meter"), is_enabled=False),
    )
    assert enabled_widget_apps(rows, {"gauge": False, "meter": True, "files": True}) == frozenset({"meter"})


def test_the_store_keeps_every_choice_and_reads_an_unusable_file_as_empty(tmp_path: Path) -> None:
    store = WidgetsStore(state_directory=tmp_path)
    assert store.read() == {}

    store.write(AppName("gauge"), False)
    store.write(AppName("meter"), True)
    store.write(AppName("gauge"), True)

    assert store.read() == {"gauge": True, "meter": True}
    assert json.loads((tmp_path / WIDGETS_FILENAME).read_text()) == {
        "version": 1,
        "apps": {"gauge": {"is_enabled": True}, "meter": {"is_enabled": True}},
    }
    (tmp_path / WIDGETS_FILENAME).write_text('{"version": 2, "apps": {}}')
    assert store.read() == {}
    (tmp_path / WIDGETS_FILENAME).write_text('{"version": 1, "apps": {"gauge": {"is_enabled": "maybe"}}}')
    assert store.read() == {}
