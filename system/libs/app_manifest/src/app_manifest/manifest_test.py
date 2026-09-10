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
from app_manifest.testing import write_app_manifest, write_repo_file

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
        "launcher_rank": 20,
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
    assert manifest.launcher_rank == 20


@pytest.mark.parametrize("rank", [0, -3, "ten", 1.5])
def test_launcher_rank_must_be_a_positive_integer(rank: object) -> None:
    with pytest.raises(ValidationError):
        AppManifest.model_validate({**_full_manifest_data(), "launcher_rank": rank})


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
    assert manifest.launcher_rank is None
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


# --- references and scope -------------------------------------------------------

_REFERENCING_MANIFEST = """
name = "news"
display_name = "News"
icon = "icon.svg"

[[references]]
path = ".agents/skills/news-refresh"
note = "Fetches stories on a schedule; calls POST /api/ingest"

[[references]]
path = "system/scripts/run_news.sh"

[scope]
exclude = ["system/apps/news/frontend/dist/**"]
"""


def test_a_manifest_reads_its_references_and_scope() -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "references": [
                {"path": ".agents/skills/news-refresh", "note": "Drives the ingest route"},
                {"path": "system/scripts/run_news.sh"},
            ],
            "scope": {"exclude": ["system/apps/news/frontend/dist/**"]},
        }
    )

    assert [reference.path for reference in manifest.references] == [
        ".agents/skills/news-refresh",
        "system/scripts/run_news.sh",
    ]
    assert manifest.references[0].note == "Drives the ingest route"
    assert manifest.references[1].note is None
    assert manifest.scope.exclude == ("system/apps/news/frontend/dist/**",)


def test_a_manifest_without_references_or_scope_takes_the_empty_defaults() -> None:
    manifest = AppManifest.model_validate(
        {"name": "news", "display_name": "News", "icon": "icon.svg"}
    )

    assert manifest.references == ()
    assert manifest.scope.exclude == ()


@pytest.mark.parametrize(
    "reference_path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../outside",
        "system/../../escape",
        ".agents/skills/*/run.py",
        "docs/system/?.md",
        "docs/[abc].md",
        "docs/a]b.md",
        ".agents/skills/news-refresh/",
        "docs\\windows.md",
        "system/vendor/mngr/libs/mngr",
        "data/.apps/news",
    ],
)
def test_reference_paths_that_are_not_literal_repo_relative_paths_are_rejected(
    reference_path: str,
) -> None:
    with pytest.raises(ValidationError):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": reference_path}],
            }
        )


def test_a_reference_path_naming_a_glob_says_so() -> None:
    with pytest.raises(ValidationError, match="glob character"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": "docs/**/news.md"}],
            }
        )


@pytest.mark.parametrize("note", ["", "   ", "two\nlines", "a carriage\rreturn", "x" * 201])
def test_a_reference_note_must_be_one_non_empty_line(note: str) -> None:
    with pytest.raises(ValidationError, match="note"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": "docs/news.md", "note": note}],
            }
        )


def test_duplicate_reference_paths_are_rejected() -> None:
    with pytest.raises(ValidationError, match="reference paths must be unique"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [
                    {"path": "docs/news.md"},
                    {"path": "docs/news.md", "note": "the same artifact again"},
                ],
            }
        )


@pytest.mark.parametrize(
    "exclude_glob", ["", "  ", "/abs/**", "../outside/**", "system\\apps\\**"]
)
def test_exclude_globs_must_be_repo_relative(exclude_glob: str) -> None:
    with pytest.raises(ValidationError, match="exclude glob"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "scope": {"exclude": [exclude_glob]},
            }
        )


def test_load_manifest_derives_the_repo_root_from_the_apps_layout(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(
        tmp_path, "news", _REFERENCING_MANIFEST, is_icon_written=True
    )
    write_repo_file(tmp_path, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    write_repo_file(tmp_path, "system/scripts/run_news.sh", "#!/bin/sh\n")

    manifest = load_manifest(manifest_path)

    assert [reference.path for reference in manifest.references] == [
        ".agents/skills/news-refresh",
        "system/scripts/run_news.sh",
    ]


def test_load_manifest_rejects_a_reference_to_something_that_does_not_exist(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(
        tmp_path, "news", _REFERENCING_MANIFEST, is_icon_written=True
    )
    write_repo_file(tmp_path, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")

    with pytest.raises(ManifestLoadError, match="run_news.sh"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_rejects_a_reference_inside_the_apps_own_directory(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = "system/apps/news/runner.py"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/news/runner.py", "\n")

    with pytest.raises(ManifestLoadError, match="own directory"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_rejects_a_reference_to_another_apps_directory(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = "system/apps/files/src"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/files/src/runner.py", "\n")

    with pytest.raises(ManifestLoadError, match="another app"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_accepts_a_reference_to_a_file_directly_under_the_apps_directory(
    tmp_path: Path,
) -> None:
    # system/apps/README.md sits beside the app directories without being one, so the
    # other-app rule (which keys on the directory a path enters) leaves it alone.
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = "system/apps/README.md"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/README.md", "# apps\n")

    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    assert [reference.path for reference in manifest.references] == ["system/apps/README.md"]


def test_load_manifest_off_the_apps_layout_skips_the_location_rules_until_a_root_is_given(
    tmp_path: Path,
) -> None:
    loose_directory = tmp_path / uuid4().hex
    loose_directory.mkdir()
    (loose_directory / "icon.svg").write_text(_ICON)
    manifest_path = loose_directory / "app.toml"
    manifest_path.write_text(_REFERENCING_MANIFEST)

    assert len(load_manifest(manifest_path).references) == 2

    with pytest.raises(ManifestLoadError, match="does not exist"):
        load_manifest(manifest_path, repo_root=tmp_path)
