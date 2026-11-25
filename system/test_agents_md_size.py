"""Hold ``AGENTS.md`` to the smallest size every harness reads whole.

A harness that reads only the start of ``AGENTS.md`` drops the rest without the agent knowing,
so an instruction past the cut is never followed. The limits, at the versions the workspace pins
(``.mngr/settings.toml``, and ``system/scripts/agy_install.sh`` for agy):

- Antigravity truncates any rule file, ``AGENTS.md`` included, past 24,000 bytes. The figure
  is fixed in agy (its rules docs, and a literal in the binary), with no setting to raise it.
- Codex reads ``project_doc_max_bytes`` bytes of project instructions, 32 KiB unless
  ``[agent_types.codex].config_overrides`` sets it.
- Claude Code, Pi and OpenCode read the file whole at any size this test allows.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_AGENTS_MD_PATH = _REPO_ROOT / "AGENTS.md"
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"

_ANTIGRAVITY_RULE_FILE_MAX_BYTES = 24_000
_CODEX_DEFAULT_PROJECT_DOC_MAX_BYTES = 32 * 1024


def _codex_project_doc_max_bytes() -> int:
    with _SETTINGS_PATH.open("rb") as settings_file:
        codex = tomllib.load(settings_file)["agent_types"]["codex"]
    return codex.get("config_overrides", {}).get("project_doc_max_bytes", _CODEX_DEFAULT_PROJECT_DOC_MAX_BYTES)


def _agents_md_max_bytes_by_harness() -> dict[str, int]:
    return {
        "antigravity": _ANTIGRAVITY_RULE_FILE_MAX_BYTES,
        "codex": _codex_project_doc_max_bytes(),
    }


def test_agents_md_fits_every_harness() -> None:
    limits = _agents_md_max_bytes_by_harness()
    tightest_harness = min(limits, key=limits.__getitem__)
    size = len(_AGENTS_MD_PATH.read_bytes())
    assert size <= limits[tightest_harness], (
        f"AGENTS.md is {size} bytes, over the {limits[tightest_harness]} bytes {tightest_harness} reads, "
        f"so that harness never sees its last {size - limits[tightest_harness]} bytes. Move detail most turns "
        "do not need into a skill or .agents/shared/references/, named at the step that needs it "
        "(docs/system/skill_authoring_guide.md)."
    )
