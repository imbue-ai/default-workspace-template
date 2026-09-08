from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import (
    AppManifest,
    ShortcutMode,
    load_manifest,
    manifest_icon_path,
)

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


def _full_manifest_data() -> dict[str, object]:
    return {
        "name": "files",
        "display_name": "File Viewer",
        "icon": "icon.svg",
        "instances": True,
        "instances_url": "http://127.0.0.1:8301",
        "critical": False,
        "priority": "files",
        "program": "files",
        "internal": False,
        "default_shortcut": {"action": "new", "mode": "focus"},
        "actions": [
            {
                "id": "new",
                "label": "New File Viewer",
                "params": [{"name": "path", "label": "Path", "required": False}],
            }
        ],
    }


def test_full_manifest_round_trips_every_field() -> None:
    manifest = AppManifest.model_validate(_full_manifest_data())

    assert manifest.name == "files"
    assert manifest.display_name == "File Viewer"
    assert manifest.icon == "icon.svg"
    assert manifest.instances is True
    assert manifest.instances_url == "http://127.0.0.1:8301"
    assert manifest.priority == "files"
    assert manifest.program == "files"
    assert manifest.default_shortcut is not None
    assert manifest.default_shortcut.mode is ShortcutMode.FOCUS
    assert [action.id for action in manifest.actions] == ["new"]
    assert manifest.actions[0].params[0].name == "path"


def test_minimal_manifest_takes_the_documented_defaults() -> None:
    manifest = AppManifest.model_validate(
        {"name": "news", "display_name": "News", "icon": "icon.svg"}
    )

    assert manifest.instances is False
    assert manifest.instances_url is None
    assert manifest.critical is False
    assert manifest.priority == "user"
    assert manifest.program == "news"
    assert manifest.internal is False
    assert manifest.default_shortcut is None
    assert manifest.actions == ()
    assert manifest.handles == {}


def test_program_defaults_to_the_name_but_an_explicit_program_wins() -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "program": "news-server",
        }
    )

    assert manifest.program == "news-server"


@pytest.mark.parametrize(
    "name",
    [
        "MyApp",
        "host-abc",
        "agent-abc",
        "-leading",
        "trailing-",
        "double--hyphen",
        "",
        "dot.name",
        "localhost",
        "auth",
        "a" * 33,
        "news\n",
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="invalid app name"):
        AppManifest.model_validate(
            {"name": name, "display_name": "X", "icon": "icon.svg"}
        )


@pytest.mark.parametrize(
    "name",
    ["terminal", "my-app", "app2", "a", "system_interface", "openvscode-server-4"],
)
def test_valid_names_are_accepted(name: str) -> None:
    assert (
        AppManifest.model_validate(
            {"name": name, "display_name": "X", "icon": "icon.svg"}
        ).name
        == name
    )


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 65])
def test_display_name_must_be_non_empty_and_at_most_64_characters(
    display_name: str,
) -> None:
    with pytest.raises(ValidationError, match="display_name"):
        AppManifest.model_validate(
            {"name": "news", "display_name": display_name, "icon": "icon.svg"}
        )


def test_icon_is_required_unless_internal() -> None:
    with pytest.raises(ValidationError, match="icon is required"):
        AppManifest.model_validate({"name": "news", "display_name": "News"})

    internal = AppManifest.model_validate(
        {"name": "owner-exec", "display_name": "Owner exec", "internal": True}
    )
    assert internal.icon is None


@pytest.mark.parametrize("icon", ["icon.png", "/abs/icon.svg", "icon"])
def test_icon_must_be_a_relative_svg_path(icon: str) -> None:
    with pytest.raises(ValidationError, match="invalid icon"):
        AppManifest.model_validate(
            {"name": "news", "display_name": "News", "icon": icon}
        )


@pytest.mark.parametrize(
    "instances_url",
    [
        "https://127.0.0.1:8301",
        "http://0.0.0.0:8301",
        "http://127.0.0.1",
        "http://127.0.0.1:8301/",
        "127.0.0.1:8301",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://127.0.0.1:8301\n",
    ],
)
def test_instances_url_must_be_a_bare_loopback_origin_with_a_usable_port(
    instances_url: str,
) -> None:
    with pytest.raises(ValidationError, match="invalid instances_url"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances": True,
                "instances_url": instances_url,
            }
        )


def test_instances_url_requires_instances() -> None:
    with pytest.raises(ValidationError, match="instances_url is only allowed"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances_url": "http://localhost:9000",
            }
        )


def test_actions_are_forbidden_for_a_single_instance_app() -> None:
    with pytest.raises(ValidationError, match="actions are only allowed"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "actions": [{"id": "new", "label": "New"}],
            }
        )


def test_duplicate_action_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances": True,
                "actions": [
                    {"id": "new", "label": "New"},
                    {"id": "new", "label": "Again"},
                ],
            }
        )


@pytest.mark.parametrize("action_id", ["New", "-new", "", "a" * 33, "new tab", "new\n"])
def test_action_ids_follow_the_id_rule(action_id: str) -> None:
    with pytest.raises(ValidationError, match="invalid action id"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances": True,
                "actions": [{"id": action_id, "label": "New"}],
            }
        )


def test_default_shortcut_must_name_a_declared_action() -> None:
    with pytest.raises(ValidationError, match="default_shortcut.action"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances": True,
                "actions": [{"id": "new", "label": "New"}],
                "default_shortcut": {"action": "other", "mode": "new"},
            }
        )


def test_default_shortcut_open_is_allowed_only_for_a_single_instance_app() -> None:
    single = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "default_shortcut": {"action": "open", "mode": "focus"},
        }
    )
    assert single.default_shortcut is not None
    assert single.default_shortcut.action == "open"

    with pytest.raises(ValidationError, match="default_shortcut.action"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instances": True,
                "actions": [{"id": "new", "label": "New"}],
                "default_shortcut": {"action": "open", "mode": "focus"},
            }
        )


def test_default_shortcut_mode_must_be_focus_or_new() -> None:
    with pytest.raises(ValidationError, match="mode"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "default_shortcut": {"action": "open", "mode": "always"},
            }
        )


def test_handles_must_be_absent_or_empty() -> None:
    assert (
        AppManifest.model_validate(
            {"name": "news", "display_name": "News", "icon": "icon.svg", "handles": {}}
        ).handles
        == {}
    )
    with pytest.raises(ValidationError, match="handles"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "handles": {"scheme": "x"},
            }
        )


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="instance_lifetime"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "instance_lifetime": "explicit",
            }
        )


def test_load_manifest_reads_a_file_and_resolves_its_icon(tmp_path: Path) -> None:
    app_dir = tmp_path / uuid4().hex
    app_dir.mkdir()
    (app_dir / "icon.svg").write_text(_ICON)
    manifest_path = app_dir / "app.toml"
    manifest_path.write_text(
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
    )

    manifest = load_manifest(manifest_path)

    assert manifest.name == "news"
    assert manifest_icon_path(manifest_path, manifest) == app_dir / "icon.svg"


def test_load_manifest_reports_a_missing_icon_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text(
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
    )

    with pytest.raises(ManifestLoadError, match="does not exist"):
        load_manifest(manifest_path)


def test_load_manifest_reports_invalid_toml_and_invalid_values(tmp_path: Path) -> None:
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("name = \n")
    with pytest.raises(ManifestLoadError, match="not valid TOML"):
        load_manifest(bad_toml)

    bad_value = tmp_path / "value.toml"
    bad_value.write_text('name = "news"\ndisplay_name = ""\nicon = "icon.svg"\n')
    with pytest.raises(ManifestLoadError, match="display_name"):
        load_manifest(bad_value)


def test_load_manifest_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestLoadError, match="cannot read"):
        load_manifest(tmp_path / "nope.toml")
