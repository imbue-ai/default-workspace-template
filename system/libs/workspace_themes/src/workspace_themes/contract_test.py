import re
from pathlib import Path

from workspace_themes.contract import COLOR_TOKENS, CORE_PARTS

# system/libs/workspace_themes/src/workspace_themes/contract_test.py -> system/libs
_LIBS_DIRECTORY = Path(__file__).resolve().parents[3]
_PARTS_TS = _LIBS_DIRECTORY / "workspace_ui" / "src" / "themes" / "parts.ts"
_BASE_CSS = _LIBS_DIRECTORY / "workspace_ui" / "src" / "base.css"


def test_the_validator_and_the_frontend_name_the_same_parts_in_the_same_order() -> None:
    parts_ts = _PARTS_TS.read_text(encoding="utf-8")
    block = parts_ts.split("export const PARTS = [", 1)[1].split("] as const;", 1)[0]

    frontend_parts = tuple(re.findall(r'"([a-z-]+)"', block))

    assert frontend_parts == CORE_PARTS


def test_every_color_token_the_design_system_declares_is_a_contract_token() -> None:
    root_block = (
        _BASE_CSS.read_text(encoding="utf-8").split(":root {", 1)[1].split("\n}", 1)[0]
    )

    declared = sorted(
        set(re.findall(r"^\s*(--c-[a-z0-9-]+):", root_block, flags=re.MULTILINE))
    )

    assert declared == sorted(COLOR_TOKENS)
