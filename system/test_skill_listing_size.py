"""Hold the skill listing to the smallest budget a harness gives it.

Every skill's name and description is in the agent's context from the first turn, and that
listing is all that tells an agent a skill exists. A harness gives the listing a budget and,
past it, cuts descriptions without the agent knowing, so the skills it cut stop being found.
The budgets, at the versions the workspace pins (``.mngr/settings.toml``):

- Claude Code gives it 30,000 characters on every model the workspace offers: 1% of the window
  it budgets against, a share ``skillListingBudgetFraction`` in ``.claude/settings.json`` sets.
  Past that, skills lose their whole description, the least used first.
- Codex gives it 10,000 tokens of 4 bytes: 2% of gpt-6.1-sol's 500,000-token window, and the
  most its ``skills.max_context_tokens`` can set. Past that, every description is shortened.
- Antigravity, Pi and OpenCode state no budget.

The same listing holds skills this repo does not: each harness's own, and those of the plugins
the workspace enables. Each reserve below is what they took in a real chat (on Codex, its table
of skill roots included), rounded up for growth.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from agents_shared.skill_checks import GUIDE_PATH, skill_dirs

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"

_CLAUDE_CODE_BUDGETED_WINDOW_CHARS = 3_000_000
_CLAUDE_CODE_DEFAULT_LISTING_FRACTION = 0.01
_CLAUDE_CODE_MAX_DESCRIPTION_CHARS = 1_536
_CLAUDE_CODE_OTHER_SKILLS_CHARS = 10_000

_CODEX_LISTING_TOKENS = 10_000
_CODEX_BYTES_PER_TOKEN = 4
_CODEX_MAX_DESCRIPTION_CHARS = 1_024
_CODEX_OTHER_SKILLS_TOKENS = 1_600


def _listed_skills() -> list[tuple[str, str]]:
    """The name and description of every skill under ``.agents/skills/``."""
    skills = []
    for skill_dir in skill_dirs(_REPO_ROOT):
        frontmatter = yaml.safe_load(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)[1]
        )
        skills.append((frontmatter["name"], frontmatter["description"]))
    return skills


def _claude_code_listing_chars(skills: list[tuple[str, str]]) -> int:
    return sum(
        len(f"- {name}: {description[:_CLAUDE_CODE_MAX_DESCRIPTION_CHARS]}\n")
        for name, description in skills
    )


def _claude_code_budget_chars() -> int:
    settings = json.loads(_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    fraction = settings.get(
        "skillListingBudgetFraction", _CLAUDE_CODE_DEFAULT_LISTING_FRACTION
    )
    return int(_CLAUDE_CODE_BUDGETED_WINDOW_CHARS * fraction)


def _codex_listing_tokens(skills: list[tuple[str, str]]) -> int:
    lines = (
        f"- {name}: {description[:_CODEX_MAX_DESCRIPTION_CHARS]} (file: r0/{name}/SKILL.md)\n"
        for name, description in skills
    )
    return sum(
        -(-len(line.encode("utf-8")) // _CODEX_BYTES_PER_TOKEN) for line in lines
    )


def test_skill_listing_fits_every_harness() -> None:
    skills = _listed_skills()
    sizes_and_room = {
        "Claude Code": (
            _claude_code_listing_chars(skills),
            _claude_code_budget_chars() - _CLAUDE_CODE_OTHER_SKILLS_CHARS,
            "characters",
        ),
        "Codex": (
            _codex_listing_tokens(skills),
            _CODEX_LISTING_TOKENS - _CODEX_OTHER_SKILLS_TOKENS,
            "tokens",
        ),
    }
    over = [
        f"  - {harness}: {size:,} {unit}, with room for {room:,}"
        for harness, (size, room, unit) in sizes_and_room.items()
        if size > room
    ]
    longest = sorted(skills, key=lambda skill: len(skill[1]), reverse=True)[:5]
    assert not over, (
        "The names and descriptions of the skills under .agents/skills/ no longer fit the listing a "
        "harness gives its agent, so that harness cuts descriptions and the agent stops finding those "
        "skills:\n"
        + "\n".join(over)
        + "\nShorten descriptions, or remove a skill nothing needs. The longest "
        "descriptions: "
        + ", ".join(
            f"{name} ({len(description)} characters)" for name, description in longest
        )
        + f". See {GUIDE_PATH}."
    )
