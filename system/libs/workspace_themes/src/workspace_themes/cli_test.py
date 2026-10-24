import json
from pathlib import Path

from click.testing import CliRunner, Result
from PIL import Image

from workspace_themes.cli import main
from workspace_themes.contract import (
    BUILTIN_THEMES_DIRECTORY,
    GENERATED_ICONS_DIRECTORY,
)
from workspace_themes.testing import (
    write_standard_theme,
    write_test_icon,
    write_test_theme,
)

_APP_TOML = 'name = "files"\ndisplay_name = "Files"\nicon = "icon.svg"\n'


def _workspace(tmp_path: Path) -> Path:
    write_standard_theme(tmp_path)
    write_test_theme(
        tmp_path / BUILTIN_THEMES_DIRECTORY,
        "paper",
        '[data-part="window"] { color: #000000; }\n',
    )
    app = tmp_path / "system" / "apps" / "files"
    app.mkdir(parents=True)
    (app / "app.toml").write_text(_APP_TOML, encoding="utf-8")
    (app / "icon.svg").write_text('<svg viewBox="0 0 216 216"/>', encoding="utf-8")
    return tmp_path


def test_validate_reports_each_theme_and_fails_on_a_problem(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runner = CliRunner()

    passing = runner.invoke(main, ["validate", "--repo-root", str(root)])
    write_test_theme(root / "themes", "classy", ".x { color: red; }\n")
    failing = runner.invoke(main, ["validate", "classy", "--repo-root", str(root)])

    assert passing.exit_code == 0 and passing.output == "standard: ok\npaper: ok\n"
    assert failing.exit_code == 1
    assert ".x is a class" in failing.output


def test_a_folder_that_is_not_a_workspace_root_is_refused(tmp_path: Path) -> None:
    runner = CliRunner()

    validated = runner.invoke(main, ["validate", "--repo-root", str(tmp_path)])
    missing = runner.invoke(main, ["icon", "missing", "--repo-root", str(tmp_path)])

    for result in (validated, missing):
        assert result.exit_code == 1
        assert f"{tmp_path.resolve()} is not a workspace root" in result.output


def test_list_names_every_theme_and_whether_it_is_available(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    write_test_theme(root / "themes", "classy", ".x { color: red; }\n")
    runner = CliRunner()

    text = runner.invoke(main, ["list", "--repo-root", str(root)])
    listed = runner.invoke(main, ["list", "--json", "--repo-root", str(root)])

    themes = root / BUILTIN_THEMES_DIRECTORY
    assert text.exit_code == 0, text.output
    assert text.output.splitlines() == [
        f"standard\tStandard\tbuiltin\tavailable\t{themes / 'standard'}",
        f"paper\tTest paper\tbuiltin\tavailable\t{themes / 'paper'}",
        f"classy\tTest classy\tworkspace\tunavailable (1 problems)\t{root / 'themes' / 'classy'}",
    ]
    assert listed.exit_code == 0, listed.output
    records = {record["id"]: record for record in json.loads(listed.output)}
    assert list(records) == ["standard", "paper", "classy"]
    assert records["paper"]["chain"] == ["standard", "paper"]
    assert records["paper"]["available"] is True and records["paper"]["problems"] == []
    assert records["classy"]["source"] == "workspace"
    assert records["classy"]["available"] is False
    assert ".x is a class" in records["classy"]["problems"][0]


def test_validate_reports_a_workspace_theme_that_repeats_a_built_in_id(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    write_test_theme(root / "themes", "paper", "")

    result = CliRunner().invoke(main, ["validate", "paper", "--repo-root", str(root)])

    builtin = root / BUILTIN_THEMES_DIRECTORY / "paper"
    workspace = root / "themes" / "paper"
    assert result.exit_code == 1
    assert result.output == (
        f"paper ({builtin}): ok\n"
        f"paper ({workspace}): 1 problems\n"
        f"  the id 'paper' is already taken by {builtin}\n"
    )


def test_new_lays_out_a_workspace_theme_that_validates(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runner = CliRunner()

    made = runner.invoke(
        main,
        [
            "new",
            "sepia",
            "--name",
            "Sepia \U0001d11e */",
            "--description",
            "Old paper, \U0001d11e.",
            "--base",
            "paper",
            "--repo-root",
            str(root),
        ],
    )
    checked = runner.invoke(main, ["validate", "sepia", "--repo-root", str(root)])
    again = runner.invoke(
        main,
        [
            "new",
            "sepia",
            "--name",
            "Sepia",
            "--description",
            "x",
            "--repo-root",
            str(root),
        ],
    )

    assert made.exit_code == 0, made.output
    assert (root / "themes" / "sepia" / "theme.toml").is_file()
    assert checked.exit_code == 0 and checked.output == "sepia: ok\n"
    assert again.exit_code != 0 and "already exists" in again.output


def test_icon_spec_lists_no_missing_icon_for_the_standard_theme(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    spec = CliRunner().invoke(
        main, ["icon", "spec", "standard", "--repo-root", str(root)]
    )

    assert spec.exit_code == 0, spec.output
    assert json.loads(spec.output)["apps_without_icon"] == []


def test_a_grid_file_that_is_not_utf8_is_refused_with_a_message(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    grid = tmp_path / "files.txt"
    grid.write_bytes(b"k\xff\n")

    drawn = CliRunner().invoke(
        main,
        [
            "icon",
            "grid",
            str(grid),
            "--theme",
            "paper",
            "--legend",
            "k=#000000",
            "--out",
            str(tmp_path / "files.png"),
            "--repo-root",
            str(root),
        ],
    )

    assert drawn.exit_code == 1
    assert "cannot be read as UTF-8 text" in drawn.output
    assert not (tmp_path / "files.png").exists()


def test_an_icon_drawn_as_a_grid_is_fitted_checked_and_installed_for_a_built_in_theme(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    grid = tmp_path / "files.txt"
    grid.write_text(
        "\n".join(
            [
                "kkkkkkkk",
                "kwwwwwwk",
                "kwkkkkwk",
                "kwkwwkwk",
                "kwkkkkwk",
                "kwwwwwwk",
                "kkkkkkkk",
            ]
        )
    )
    fitted = tmp_path / "files.png"
    runner = CliRunner()

    spec = runner.invoke(main, ["icon", "spec", "paper", "--repo-root", str(root)])
    drawn = runner.invoke(
        main,
        [
            "icon",
            "grid",
            str(grid),
            "--theme",
            "paper",
            "--legend",
            "k=#000000,w=#ffffff",
            "--out",
            str(fitted),
            "--preview",
            str(tmp_path / "files-preview.png"),
            "--repo-root",
            str(root),
        ],
    )
    installed = runner.invoke(
        main,
        [
            "icon",
            "install",
            str(fitted),
            "--theme",
            "paper",
            "--app",
            "files",
            "--repo-root",
            str(root),
        ],
    )
    spec_after = runner.invoke(
        main, ["icon", "spec", "paper", "--repo-root", str(root)]
    )

    assert json.loads(spec.output)["apps_without_icon"] == ["files"]
    assert drawn.exit_code == 0, drawn.output
    assert Image.open(tmp_path / "files-preview.png").width >= 256
    assert installed.exit_code == 0, installed.output
    assert (
        Path(installed.output.strip())
        == root / GENERATED_ICONS_DIRECTORY / "paper" / "icons" / "files.png"
    )
    assert json.loads(spec_after.output)["apps_without_icon"] == []


def test_an_icon_already_in_place_is_installed_again_as_it_is(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    drawn = tmp_path / "files.png"
    write_test_icon(drawn, 32)
    runner = CliRunner()
    install = [
        "icon",
        "install",
        "--theme",
        "paper",
        "--app",
        "files",
        "--repo-root",
        str(root),
    ]

    first = runner.invoke(main, [*install, str(drawn)])
    installed = Path(first.output.strip())
    again = runner.invoke(main, [*install, str(installed)])

    assert first.exit_code == 0, first.output
    assert again.exit_code == 0, again.output
    assert again.output == first.output
    assert installed.read_bytes() == drawn.read_bytes()


def test_an_icon_that_misses_the_limits_is_not_installed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    wrong_size = tmp_path / "files.png"
    write_test_icon(wrong_size, 48)
    all_background = tmp_path / "flat.txt"
    all_background.write_text("rrrr\nrrrr\n")
    runner = CliRunner()

    installed = runner.invoke(
        main,
        [
            "icon",
            "install",
            str(wrong_size),
            "--theme",
            "paper",
            "--app",
            "files",
            "--repo-root",
            str(root),
        ],
    )
    drawn = runner.invoke(
        main,
        [
            "icon",
            "grid",
            str(all_background),
            "--theme",
            "paper",
            "--legend",
            "r=#ff0000",
            "--out",
            str(tmp_path / "flat.png"),
            "--repo-root",
            str(root),
        ],
    )

    assert installed.exit_code == 1
    assert "the icon is 48x48; the theme's icons are 32x32" in installed.output
    assert not (root / GENERATED_ICONS_DIRECTORY).exists()
    assert (
        drawn.exit_code == 1 and "empty once its background is cleared" in drawn.output
    )


def test_a_pixel_grid_is_refused_for_a_theme_that_draws_svg_icons(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    grid = tmp_path / "grid.txt"
    grid.write_text("kk\nkk\n")

    drawn = CliRunner().invoke(
        main,
        [
            "icon",
            "grid",
            str(grid),
            "--theme",
            "standard",
            "--legend",
            "k=#000000",
            "--out",
            str(tmp_path / "grid.png"),
            "--repo-root",
            str(root),
        ],
    )

    assert drawn.exit_code == 1
    assert "draws svg icons" in drawn.output
    assert not (tmp_path / "grid.png").exists()


def test_an_icon_that_is_not_a_png_is_not_installed_for_a_png_theme(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    drawn = tmp_path / "drawn.png"
    write_test_icon(drawn, 32)
    disguised = tmp_path / "files.png"
    with Image.open(drawn) as image:
        image.save(disguised, format="WEBP", lossless=True)

    installed = CliRunner().invoke(
        main,
        [
            "icon",
            "install",
            str(disguised),
            "--theme",
            "paper",
            "--app",
            "files",
            "--repo-root",
            str(root),
        ],
    )

    assert installed.exit_code == 1
    assert "is WEBP, not PNG" in installed.output
    assert not (root / GENERATED_ICONS_DIRECTORY).exists()


def test_an_icon_a_built_in_theme_ships_is_not_installed_over_but_a_workspace_theme_takes_it(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    write_test_theme(
        root / BUILTIN_THEMES_DIRECTORY, "boxed", "", app_names_with_icons=("files",)
    )
    write_test_theme(root / "themes", "my-boxed", "", base="boxed")
    icon = tmp_path / "icon.png"
    write_test_icon(icon, 32)
    runner = CliRunner()

    def install(theme_id: str, app_name: str) -> Result:
        return runner.invoke(
            main,
            [
                "icon",
                "install",
                str(icon),
                "--theme",
                theme_id,
                "--app",
                app_name,
                "--repo-root",
                str(root),
            ],
        )

    shipped_app_icon = install("boxed", "files")
    shipped_generic_icon = install("paper", "app")
    own_generic_icon = install("my-boxed", "app")

    assert shipped_app_icon.exit_code == 1
    assert "ships its own 'files' icon" in shipped_app_icon.output
    assert shipped_generic_icon.exit_code == 1
    assert not (root / GENERATED_ICONS_DIRECTORY).exists()
    assert own_generic_icon.exit_code == 0, own_generic_icon.output
    assert (
        Path(own_generic_icon.output.strip())
        == root / "themes" / "my-boxed" / "icons" / "app.png"
    )


def test_lint_app_finds_literal_looks_in_a_tokens_mode_app(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    source = root / "system" / "apps" / "files" / "frontend" / "src"
    source.mkdir(parents=True)
    (source / "view.ts").write_text(
        'const OK = "bg-surface text-primary";\n// a #fff comment is fine\nconst BAD = "bg-[#ff0000]";\n',
        encoding="utf-8",
    )
    (source / "style.css").write_text(
        ".x { font-family: Comic Sans; }\n.y { color: var(--c-accent); }\n"
        ".z { font-family: var(--font-sans); }\nbutton { font-family: inherit; }\n"
        ".r { box-shadow: var(--c-shadow-raised); }\n.n { box-shadow: none; }\n"
        ".s { box-shadow: 0 1px 2px black; }\n* { box-shadow: 0 0 2px black; }\n"
        " * a #fff comment line is fine\n*[data-x] { box-shadow: 0 0 2px black; }\n"
        "*.a { color: #ffffff; }\n",
        "utf-8",
    )

    linted = CliRunner().invoke(main, ["lint-app", "files", "--repo-root", str(root)])

    assert linted.exit_code == 1
    assert "frontend/src/style.css:1: a font family" in linted.output
    assert "frontend/src/view.ts:3: a literal color" in linted.output
    assert "view.ts:2" not in linted.output
    assert "frontend/src/style.css:7: a literal shadow" in linted.output
    assert "frontend/src/style.css:8: a literal shadow" in linted.output
    assert "style.css:9" not in linted.output
    assert "frontend/src/style.css:10: a literal shadow" in linted.output
    assert "frontend/src/style.css:11: a literal color" in linted.output
    assert not any(f"style.css:{line}:" in linted.output for line in range(3, 7))


def test_lint_app_reads_the_markup_a_python_module_builds_but_not_its_comments_or_tests(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    package = root / "system" / "apps" / "files" / "files_app"
    package.mkdir(parents=True)
    (package / "runner.py").write_text(
        "# the page is built here, not from #fff literals\nPAGE = \"<p style='color: #336699'>hi</p>\"\n",
        encoding="utf-8",
    )
    (package / "runner_test.py").write_text('COLOR = "#ff0000"\n', encoding="utf-8")

    linted = CliRunner().invoke(main, ["lint-app", "files", "--repo-root", str(root)])

    assert linted.exit_code == 1
    assert "files_app/runner.py:2: a literal color" in linted.output
    assert "runner.py:1" not in linted.output
    assert "runner_test.py" not in linted.output


def test_icon_missing_lists_the_apps_a_theme_draws_no_icon_for_and_skips_themes_on_the_standard_icons(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runner = CliRunner()
    made = runner.invoke(
        main,
        [
            "new",
            "plain",
            "--name",
            "Plain",
            "--description",
            "The standard icons.",
            "--repo-root",
            str(root),
        ],
    )

    missing = runner.invoke(main, ["icon", "missing", "--repo-root", str(root)])
    write_test_theme(
        root / BUILTIN_THEMES_DIRECTORY,
        "paper",
        '[data-part="window"] { color: #000000; }\n',
        app_names_with_icons=("files",),
    )
    covered = runner.invoke(main, ["icon", "missing", "--repo-root", str(root)])

    assert made.exit_code == 0, made.output
    assert missing.exit_code == 1
    assert missing.output == "paper: files\n"
    assert covered.exit_code == 0
    assert covered.output == "every theme has an icon for every app\n"


def test_icon_missing_reports_each_folder_of_a_repeated_id(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    write_test_theme(root / "themes", "paper", "", app_names_with_icons=("files",))

    missing = CliRunner().invoke(main, ["icon", "missing", "--repo-root", str(root)])

    assert missing.exit_code == 1
    assert missing.output == (
        f"paper ({root / BUILTIN_THEMES_DIRECTORY / 'paper'}): files\n"
    )


def test_a_theme_with_its_own_icons_draws_its_generic_icon_with_the_icon_tools(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    folder = write_test_theme(root / "themes", "inky", "")
    (folder / "icons" / "app.png").unlink()
    grid = tmp_path / "app.txt"
    grid.write_text("kkkk\nkwwk\nkwwk\nkkkk\n")
    drawn = tmp_path / "app.png"
    runner = CliRunner()

    unavailable = runner.invoke(main, ["validate", "inky", "--repo-root", str(root)])
    missing = runner.invoke(main, ["icon", "missing", "--repo-root", str(root)])
    gridded = runner.invoke(
        main,
        [
            "icon",
            "grid",
            str(grid),
            "--theme",
            "inky",
            "--legend",
            "k=#000000,w=#ffffff",
            "--out",
            str(drawn),
            "--repo-root",
            str(root),
        ],
    )
    installed = runner.invoke(
        main,
        [
            "icon",
            "install",
            str(drawn),
            "--theme",
            "inky",
            "--app",
            "app",
            "--repo-root",
            str(root),
        ],
    )
    available = runner.invoke(main, ["validate", "inky", "--repo-root", str(root)])

    assert unavailable.exit_code == 1
    assert missing.exit_code == 1 and "inky: app, files\n" in missing.output
    assert gridded.exit_code == 0, gridded.output
    assert installed.exit_code == 0, installed.output
    assert Path(installed.output.strip()) == folder / "icons" / "app.png"
    assert available.exit_code == 0, available.output


def test_a_theme_that_does_not_exist_is_named_plainly(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    bundled = CliRunner().invoke(main, ["bundle", "nosuch", "--repo-root", str(root)])

    assert bundled.exit_code == 1
    assert bundled.output == "Error: there is no theme 'nosuch'\n"
