"""The theme contract's two copies agree: the parts and color tokens the shared UI library's frontend names, and the
ones the theme validator (``workspace_themes.contract``) holds a theme to.

Here rather than in either library, since the shell builds the one and depends on the other: a change to either side
selects these tests.
"""

import re
from typing import Final

import pytest
from workspace_themes.contract import COLOR_TOKENS
from workspace_themes.contract import CORE_PARTS

from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY

_WORKSPACE_UI_SOURCE: Final = WORKSPACE_ROOT_DIRECTORY / "system" / "libs" / "workspace_ui" / "src"


@pytest.mark.frontend
def test_the_validator_and_the_frontend_name_the_same_parts_in_the_same_order() -> None:
    parts_ts = (_WORKSPACE_UI_SOURCE / "themes" / "parts.ts").read_text(encoding="utf-8")
    block = parts_ts.split("export const PARTS = [", 1)[1].split("] as const;", 1)[0]

    frontend_parts = tuple(re.findall(r'"([a-z-]+)"', block))

    assert frontend_parts == CORE_PARTS


@pytest.mark.frontend
def test_the_design_system_and_the_validator_name_the_same_color_tokens() -> None:
    root_block = (
        (_WORKSPACE_UI_SOURCE / "base.css").read_text(encoding="utf-8").split(":root {", 1)[1].split("\n}", 1)[0]
    )

    declared = sorted(set(re.findall(r"^\s*(--c-[a-z0-9-]+):", root_block, flags=re.MULTILINE)))

    assert declared == sorted(COLOR_TOKENS)
