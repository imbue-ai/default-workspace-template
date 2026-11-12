import json
from pathlib import Path
from typing import Final

import pytest

from workspace_themes.workspace_color import (
    IMBUE_PRIMARY,
    ON_KEY_CONTRAST,
    WorkspaceColorRecord,
    contrast,
    derive_workspace_keys,
    harmonize,
    measure,
    normalize_hex,
    read_workspace_color,
    render_workspace_color_css,
    workspace_color_css,
    workspace_color_revision,
    write_workspace_color,
)

# The keys the prototype's own seedTheme (mind-sketches prototypes/color-themes, src/theme/seedTheme.ts) derives for
# Imbue Studio's workspace palette and a handful of extremes, captured by running it once.
_GOLDEN: Final[dict[str, dict[str, str]]] = json.loads(
    (Path(__file__).parent / "testdata" / "workspace_color_golden.json").read_text(
        encoding="utf-8"
    )
)
_GOLDEN_SEEDS: Final[list[str]] = sorted(
    seed for seed in _GOLDEN if seed != IMBUE_PRIMARY
)


@pytest.mark.parametrize("seed", _GOLDEN_SEEDS)
def test_the_keys_match_the_prototype_the_brand_was_designed_in(seed: str) -> None:
    keys = derive_workspace_keys(seed)

    assert keys is not None
    assert {
        "--c-neutral-key": keys.neutral,
        "--c-primary-key": keys.primary,
        "--c-accent-key": keys.accent,
        "--c-success-key": keys.success,
        "--c-warn-key": keys.warn,
        "--c-danger-key": keys.danger,
        "--c-primary-content": keys.primary_content,
    } == {
        name: value
        for name, value in _GOLDEN[seed].items()
        if name != "--c-wallpaper-key"
    }


@pytest.mark.parametrize(
    "seed", [*_GOLDEN_SEEDS, "#000000", "#ffffff", "#7f00ff", "#00ffff"]
)
def test_white_text_reads_on_every_derived_key(seed: str) -> None:
    keys = derive_workspace_keys(seed)

    assert keys is not None
    for key in (keys.accent, keys.success, keys.warn, keys.danger):
        assert contrast("#ffffff", key) >= ON_KEY_CONTRAST - 0.01


def test_the_picked_color_is_the_primary_exactly_and_its_text_reads_on_it() -> None:
    keys = derive_workspace_keys("#cecd0c")

    assert keys is not None
    assert keys.primary == "#cecd0c"
    assert contrast(keys.primary_content, "#cecd0c") >= ON_KEY_CONTRAST


def test_a_status_hue_leans_toward_the_seed_by_at_most_fifteen_degrees() -> None:
    assert harmonize(155.0, 200.0) == pytest.approx(170.0)
    assert harmonize(155.0, 165.0) == pytest.approx(160.0)
    assert harmonize(10.0, 350.0) == pytest.approx(0.0)
    assert harmonize(60.0, 60.0) == pytest.approx(60.0)


def test_imbue_s_own_color_keeps_the_standard_keys() -> None:
    assert derive_workspace_keys(IMBUE_PRIMARY) is None
    imbue_css = render_workspace_color_css(None, "imbue")
    assert "-key:" not in imbue_css
    assert (
        '--desk-studio-wallpaper: url("/wallpapers/bundled/studio?v=imbue");'
        in imbue_css
    )
    assert workspace_color_revision(WorkspaceColorRecord(hex=IMBUE_PRIMARY)) == "imbue"
    assert workspace_color_revision(WorkspaceColorRecord(hex=None)) == "imbue"


def test_another_color_sets_every_key_and_the_wallpaper_follows_the_primary() -> None:
    css = workspace_color_css(WorkspaceColorRecord(hex="#0b292b"))

    for token in ("neutral", "primary", "accent", "success", "warn", "danger"):
        assert f"--c-{token}-key: " in css
    assert "--c-primary-key: #0b292b;" in css
    assert "--c-wallpaper-key: var(--c-primary-key);" in css
    assert (
        '--desk-studio-wallpaper: url("/wallpapers/bundled/studio?v=%230b292b");' in css
    )
    assert workspace_color_revision(WorkspaceColorRecord(hex="#0b292b")) == "#0b292b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#ABC", "#aabbcc"),
        ("0b292b", "#0b292b"),
        (" #F50D00 ", "#f50d00"),
        ("red", None),
        ("#12345", None),
    ],
)
def test_a_color_is_read_as_a_canonical_hex(value: str, expected: str | None) -> None:
    assert normalize_hex(value) == expected


def test_a_kept_color_reads_back_and_a_broken_file_reads_as_imbue_s_own(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".state" / "workspace_color.json"

    assert read_workspace_color(state_path) == WorkspaceColorRecord(hex=None)
    write_workspace_color(state_path, WorkspaceColorRecord(hex="#3c3d06"))
    assert read_workspace_color(state_path) == WorkspaceColorRecord(hex="#3c3d06")
    state_path.write_text("{not json", encoding="utf-8")
    assert read_workspace_color(state_path) == WorkspaceColorRecord(hex=None)
    state_path.write_text('{"hex": "chartreuse"}', encoding="utf-8")
    assert read_workspace_color(state_path) == WorkspaceColorRecord(hex=None)


def test_measure_round_trips_white_and_black() -> None:
    assert measure("#ffffff").oklch.lightness == pytest.approx(1.0, abs=1e-4)
    assert measure("#000000").oklch.lightness == pytest.approx(0.0, abs=1e-4)
