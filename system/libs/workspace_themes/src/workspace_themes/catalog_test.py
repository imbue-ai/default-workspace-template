from pathlib import Path

from app_manifest.primitives import AppName

from workspace_themes.catalog import (
    CachingThemeCatalogLoader,
    ThemeRoot,
    default_theme_roots,
    read_theme_catalog,
)
from workspace_themes.contract import (
    BUILTIN_THEMES_DIRECTORY,
    GENERATED_ICONS_DIRECTORY,
    MAX_BASE_CHAIN_DEPTH,
    WORKSPACE_THEMES_DIRECTORY,
)
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.primitives import ChromeSlot, ThemeSource
from workspace_themes.testing import (
    write_standard_theme,
    write_test_icon,
    write_test_theme,
)

_PARTS_CSS = '[data-part="window"] { color: #000000; }\n'


def _catalog(repo_root: Path) -> ThemeCatalog:
    return read_theme_catalog(
        default_theme_roots(repo_root),
        {AppName("chat"): frozenset({"user-message"}), AppName("files"): frozenset()},
        repo_root / GENERATED_ICONS_DIRECTORY,
    )


def test_themes_are_listed_standard_first_then_built_in_then_the_workspace_s_own(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / WORKSPACE_THEMES_DIRECTORY, "alpha", _PARTS_CSS)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "zeta", _PARTS_CSS)

    catalog = _catalog(tmp_path)

    assert [(entry.id, entry.source) for entry in catalog.entries] == [
        ("standard", ThemeSource.BUILTIN),
        ("zeta", ThemeSource.BUILTIN),
        ("alpha", ThemeSource.WORKSPACE),
    ]
    assert all(entry.is_available for entry in catalog.entries)


def test_a_theme_resolves_through_its_bases_style_files_chrome_and_icons(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    builtin = tmp_path / BUILTIN_THEMES_DIRECTORY
    write_test_theme(builtin, "paper", _PARTS_CSS, app_names_with_icons=("files",))
    child = tmp_path / WORKSPACE_THEMES_DIRECTORY / "dark-paper"
    child.mkdir(parents=True)
    (child / "theme.toml").write_text(
        'contract = 1\nid = "dark-paper"\nname = "Dark paper"\ndescription = "Paper at night."\n'
        'base = "paper"\n\n[styles]\nfiles = ["dark.css"]\n\n[chrome]\ntrailing = ["minimize", "maximize"]\n',
        encoding="utf-8",
    )
    (child / "dark.css").write_text(":root { --c-bg: #000000; }\n", encoding="utf-8")

    entry = _catalog(tmp_path).find_available("dark-paper")

    assert entry is not None
    assert entry.chain == ("standard", "paper", "dark-paper")
    assert [(str(item.theme_id), str(item.path)) for item in entry.style_files] == [
        ("paper", "parts.css"),
        ("dark-paper", "dark.css"),
    ]
    assert entry.chrome is not None and entry.chrome.leading[0] == ChromeSlot.CLOSE
    assert entry.chrome.trailing == (ChromeSlot.MINIMIZE, ChromeSlot.MAXIMIZE)
    # The icons come from the nearest theme that declares them, with that theme's curated files.
    assert entry.icons is not None and entry.icons.spec_folder == builtin / "paper"
    assert set(entry.icons.curated_by_app) == {"files"}


def test_a_theme_that_inherits_its_icon_spec_draws_its_own_icons_before_its_base_s(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    builtin = tmp_path / BUILTIN_THEMES_DIRECTORY
    write_test_theme(
        builtin, "paper", _PARTS_CSS, app_names_with_icons=("files", "chat")
    )
    child = tmp_path / WORKSPACE_THEMES_DIRECTORY / "dark-paper"
    child.mkdir(parents=True)
    (child / "theme.toml").write_text(
        'contract = 1\nid = "dark-paper"\nname = "Dark paper"\ndescription = "Paper at night."\n'
        'base = "paper"\n',
        encoding="utf-8",
    )
    write_test_icon(child / "icons" / "chat.png", 32)
    write_test_icon(child / "icons" / "app.png", 32)

    entry = _catalog(tmp_path).find_available("dark-paper")

    assert entry is not None and entry.icons is not None
    assert entry.icons.spec_folder == builtin / "paper"
    assert entry.icons.curated_by_app == {
        "files": builtin / "paper" / "icons" / "files.png",
        "chat": child / "icons" / "chat.png",
    }
    assert entry.icons.fallback == child / "icons" / "app.png"


def test_a_broken_chain_makes_a_theme_unavailable_with_the_reason(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    root = tmp_path / BUILTIN_THEMES_DIRECTORY
    write_test_theme(root, "orphan", _PARTS_CSS, base="nowhere")
    write_test_theme(root, "loop-a", _PARTS_CSS, base="loop-b")
    write_test_theme(root, "loop-b", _PARTS_CSS, base="loop-a")
    write_test_theme(root, "classy", ".title-bar { color: red; }\n")
    write_test_theme(root, "heir", _PARTS_CSS, base="classy")

    catalog = _catalog(tmp_path)

    def problems_of(theme_id: str) -> tuple[str, ...]:
        entry = catalog.find(theme_id)
        assert entry is not None
        return entry.problems

    assert problems_of("orphan") == ("base: there is no theme 'nowhere'",)
    assert "goes round in a circle" in problems_of("loop-a")[0]
    assert any(".title-bar is a class" in problem for problem in problems_of("classy"))
    assert problems_of("heir") == ("base: 'classy' is unavailable",)
    assert catalog.find_available("heir") is None


def test_a_workspace_theme_may_not_take_a_built_in_theme_s_id(tmp_path: Path) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    write_test_theme(tmp_path / WORKSPACE_THEMES_DIRECTORY, "paper", _PARTS_CSS)

    catalog = _catalog(tmp_path)

    builtin, workspace = [entry for entry in catalog.entries if entry.id == "paper"]
    assert builtin.is_available and builtin.source == ThemeSource.BUILTIN
    assert not workspace.is_available
    assert "already taken" in workspace.problems[-1]


def test_an_app_overlay_styles_only_the_parts_its_app_declares_and_is_left_out_for_an_absent_app(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    folder = write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    manifest = (folder / "theme.toml").read_text(encoding="utf-8")
    (folder / "theme.toml").write_text(
        manifest.replace(
            'files = ["parts.css"]',
            'files = ["parts.css"]\napps = ["chat", "files", "notes"]',
        ),
        encoding="utf-8",
    )
    (folder / "apps").mkdir()
    (folder / "apps" / "chat.css").write_text(
        '[data-part="chat.user-message"] { color: #000000; }\n', "utf-8"
    )
    (folder / "apps" / "files.css").write_text(
        '[data-part="files.row"] { color: #000000; }\n', "utf-8"
    )

    entry = _catalog(tmp_path).find("paper")

    assert entry is not None
    # files is here but declares no parts; notes is not here at all, so its overlay is never read or loaded.
    assert entry.problems == (
        "apps/files.css:1: 'files.row' is not a part this file may style",
    )
    assert [str(item.path) for item in entry.style_files] == [
        "parts.css",
        "apps/chat.css",
        "apps/files.css",
    ]


def test_generated_icons_count_for_a_built_in_theme_and_change_its_revision(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    before = _catalog(tmp_path).find_available("paper")

    write_test_icon(
        tmp_path / GENERATED_ICONS_DIRECTORY / "paper" / "icons" / "notes.png", 32
    )
    after = _catalog(tmp_path).find_available("paper")

    assert before is not None and after is not None
    assert after.revision != before.revision
    assert after.icons is not None and set(after.icons.generated_by_app) == {"notes"}


def test_a_theme_that_inherits_its_icon_spec_draws_the_icons_generated_for_its_base(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    child = tmp_path / WORKSPACE_THEMES_DIRECTORY / "dark-paper"
    child.mkdir(parents=True)
    (child / "theme.toml").write_text(
        'contract = 1\nid = "dark-paper"\nname = "Dark paper"\ndescription = "Paper at night."\n'
        'base = "paper"\n',
        encoding="utf-8",
    )
    before = _catalog(tmp_path).find_available("dark-paper")

    generated = tmp_path / GENERATED_ICONS_DIRECTORY / "paper" / "icons" / "notes.png"
    write_test_icon(generated, 32)
    after = _catalog(tmp_path).find_available("dark-paper")

    assert before is not None and after is not None
    assert after.revision != before.revision
    assert after.icons is not None and after.icons.generated_by_app == {
        "notes": generated
    }


def test_the_caching_loader_reads_again_only_after_a_change(tmp_path: Path) -> None:
    write_standard_theme(tmp_path)
    loader = CachingThemeCatalogLoader(repo_root=tmp_path)

    first = loader.load()
    assert loader.load() is first

    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    second = loader.load()

    assert second is not first
    assert second.find_available("paper") is not None


def test_a_missing_root_reads_as_no_themes(tmp_path: Path) -> None:
    roots = (ThemeRoot(directory=tmp_path / "nowhere", source=ThemeSource.WORKSPACE),)

    assert read_theme_catalog(roots, {}, tmp_path / "icons").entries == ()


def test_a_chain_holds_at_most_the_limit_of_themes_counting_standard_and_the_theme(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    root = tmp_path / BUILTIN_THEMES_DIRECTORY
    # standard -> level-1 -> level-2 -> ...: level-n's chain holds n + 1 themes.
    for level in range(1, MAX_BASE_CHAIN_DEPTH + 1):
        base = "standard" if level == 1 else f"level-{level - 1}"
        write_test_theme(root, f"level-{level}", _PARTS_CSS, base=base)

    catalog = _catalog(tmp_path)

    deepest_allowed = catalog.find_available(f"level-{MAX_BASE_CHAIN_DEPTH - 1}")
    too_deep = catalog.find(f"level-{MAX_BASE_CHAIN_DEPTH}")
    assert deepest_allowed is not None
    assert len(deepest_allowed.chain) == MAX_BASE_CHAIN_DEPTH
    assert too_deep is not None
    assert too_deep.problems == (
        f"base: the chain is more than {MAX_BASE_CHAIN_DEPTH} themes deep, counting standard and this theme",
    )


def test_a_theme_that_repeats_a_built_in_id_is_resolved_from_its_own_folder(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    workspace = write_test_theme(
        tmp_path / WORKSPACE_THEMES_DIRECTORY, "paper", _PARTS_CSS
    )
    manifest = workspace / "theme.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'files = ["parts.css"]', 'files = ["mine.css"]'
        ),
        encoding="utf-8",
    )
    (workspace / "mine.css").write_text(_PARTS_CSS, encoding="utf-8")

    entries = [entry for entry in _catalog(tmp_path).entries if entry.id == "paper"]

    assert [entry.folder for entry in entries] == [
        tmp_path / BUILTIN_THEMES_DIRECTORY / "paper",
        workspace,
    ]
    assert [str(item.path) for item in entries[1].style_files] == ["mine.css"]
    assert entries[1].icons is not None and entries[1].icons.spec_folder == workspace


def test_a_theme_folder_that_is_a_link_is_unavailable_and_never_read(
    tmp_path: Path,
) -> None:
    write_standard_theme(tmp_path)
    elsewhere = write_test_theme(tmp_path / "elsewhere", "paper", _PARTS_CSS)
    (tmp_path / WORKSPACE_THEMES_DIRECTORY).mkdir()
    (tmp_path / WORKSPACE_THEMES_DIRECTORY / "paper").symlink_to(elsewhere)

    entry = _catalog(tmp_path).find("paper")

    assert entry is not None
    assert entry.manifest is None
    assert entry.problems == ("a theme folder may not be a link",)


def test_the_caching_loader_sees_a_linked_theme_folder_appear(tmp_path: Path) -> None:
    write_standard_theme(tmp_path)
    elsewhere = write_test_theme(tmp_path / "elsewhere", "paper", _PARTS_CSS)
    (tmp_path / WORKSPACE_THEMES_DIRECTORY).mkdir()
    loader = CachingThemeCatalogLoader(repo_root=tmp_path)

    before = loader.load()
    (tmp_path / WORKSPACE_THEMES_DIRECTORY / "paper").symlink_to(elsewhere)
    after = loader.load()

    assert before.find("paper") is None
    assert after.find("paper") is not None


def test_linked_icons_are_left_out(tmp_path: Path) -> None:
    write_standard_theme(tmp_path)
    write_test_theme(tmp_path / BUILTIN_THEMES_DIRECTORY, "paper", _PARTS_CSS)
    outside_icon = tmp_path / "outside" / "secret.png"
    write_test_icon(outside_icon, 32)
    generated = tmp_path / GENERATED_ICONS_DIRECTORY
    (generated / "paper" / "icons").mkdir(parents=True)
    (generated / "paper" / "icons" / "notes.png").symlink_to(outside_icon)
    write_test_icon(generated / "paper" / "icons" / "chat.png", 32)
    # A linked theme folder under data/.themes/ takes every icon in it outside.
    (tmp_path / "outside" / "icons").mkdir()
    (tmp_path / "outside" / "icons" / "files.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"/>', encoding="utf-8"
    )
    (generated / "standard").symlink_to(tmp_path / "outside")

    paper = _catalog(tmp_path).find_available("paper")
    standard = _catalog(tmp_path).find_available("standard")

    assert paper is not None and paper.icons is not None
    assert set(paper.icons.generated_by_app) == {"chat"}
    assert standard is not None and standard.icons is not None
    assert standard.icons.generated_by_app == {}
