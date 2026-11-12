from pathlib import Path

from workspace_themes.contract import CORE_PARTS, THEME_MANIFEST_FILENAME
from workspace_themes.migration import (
    is_contract_1_theme,
    migrate_manifest_text,
    migrate_style_text,
    migrate_theme_folder,
)
from workspace_themes.validation import StyleSheetContext, check_style_sheet


def test_declared_and_referenced_tokens_take_their_contract_2_names() -> None:
    result = migrate_style_text(
        ":root {\n  --c-bg: #fff;\n  --c-text-primary: #000;\n}\n"
        '[data-part="badge"] { color: var(--c-text-faint); border-color: var(--c-border-strong); }\n'
    )

    assert result.text == (
        ":root {\n  --c-body-surface: #fff;\n  --c-body-content: #000;\n}\n"
        '[data-part="badge"] { color: var(--c-body-content-tertiary); border-color: var(--c-border-default); }\n'
    )
    assert result.dropped == ()


def test_a_declared_hover_token_is_dropped_and_a_referenced_one_reads_its_surface() -> (
    None
):
    result = migrate_style_text(
        ":root { --c-accent: #000; --c-accent-hover: #111; }\n"
        '[data-part="button"]:hover { background: var(--c-accent-hover); }\n'
    )

    assert "--c-accent-surface: #000;" in result.text
    assert "--c-accent-hover: #111" not in result.text
    assert "background: var(--c-accent-surface);" in result.text
    assert result.dropped == ("--c-accent-hover",)


def test_a_longer_name_is_not_read_as_a_shorter_one() -> None:
    result = migrate_style_text(
        ":root { --c-bg-sidebar: #eee; --c-surface-secondary: #ddd; --c-theme-bg: 1; }"
    )

    assert "--c-body-surface-secondary: #eee;" in result.text
    assert "--c-body-surface-secondary: #ddd;" in result.text
    assert "--c-theme-bg: 1;" in result.text


def test_only_a_contract_1_manifest_line_moves() -> None:
    assert (
        migrate_manifest_text('contract = 1\nid = "x"\n') == 'contract = 2\nid = "x"\n'
    )
    assert (
        migrate_manifest_text('contract = 2\nid = "x"\n') == 'contract = 2\nid = "x"\n'
    )
    assert migrate_manifest_text('id = "contract = 1"\n') == 'id = "contract = 1"\n'


def _contract_1_theme(folder: Path) -> Path:
    (folder / "apps").mkdir(parents=True)
    (folder / THEME_MANIFEST_FILENAME).write_text(
        'contract = 1\nid = "paper"\nname = "Paper"\ndescription = "d"\n',
        encoding="utf-8",
    )
    (folder / "tokens.css").write_text(
        ":root { --c-accent: #123456; --c-accent-hover: #000; }\n", encoding="utf-8"
    )
    (folder / "apps" / "chat.css").write_text(
        '[data-part="chat.composer"] { background: var(--c-bg-composer); }\n',
        encoding="utf-8",
    )
    return folder


def test_a_migrated_folder_validates_and_a_second_run_changes_nothing(
    tmp_path: Path,
) -> None:
    folder = _contract_1_theme(tmp_path / "paper")

    assert is_contract_1_theme(folder)
    assert migrate_theme_folder(folder) == ("--c-accent-hover",)
    after_first = {
        path.name: path.read_text(encoding="utf-8")
        for path in folder.rglob("*")
        if path.is_file()
    }
    assert not is_contract_1_theme(folder)
    assert migrate_theme_folder(folder) == ()
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in folder.rglob("*")
        if path.is_file()
    } == after_first
    assert "var(--c-body-surface-raised)" in after_first["chat.css"]
    problems = check_style_sheet(
        after_first["tokens.css"],
        StyleSheetContext(
            label="tokens.css",
            folder=folder,
            file_directory="",
            allowed_parts=frozenset(CORE_PARTS),
        ),
    )
    assert problems == []
