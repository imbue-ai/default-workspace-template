"""Every skill the workspace holds to its checks passes ``validate_skill``, and every file
under ``.agents/shared/`` is reached from something.

The rules and the reasons for them are in ``docs/system/skill_authoring_guide.md``. A skill
listed in ``.agents/third-party-skills.toml`` is kept as it came and is not checked.
"""

from __future__ import annotations

from pathlib import Path

from agents_shared.skill_checks import (
    GUIDE_PATH,
    SkillTree,
    skill_dirs,
    unreached_shared_files,
)
from agents_shared.validate_skill import third_party_skills, validate

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_skill_passes_the_skill_checks() -> None:
    tree = SkillTree(_REPO_ROOT)
    listed = third_party_skills(_REPO_ROOT / ".agents")
    errors = {
        skill_dir.name: error
        for skill_dir in skill_dirs(_REPO_ROOT)
        if skill_dir.name not in listed
        and (error := validate(skill_dir, tree=tree)) is not None
    }
    assert not errors, "These skills do not pass validate_skill.py:\n\n" + "\n\n".join(
        f"{name}: {error}" for name, error in errors.items()
    )


def test_every_shared_file_is_reached() -> None:
    unreached = unreached_shared_files(_REPO_ROOT)
    assert not unreached, (
        "Nothing reaches these files under .agents/shared/: no skill's SKILL.md links them "
        "within its crawl, AGENTS.md does not, and no program under system/ names them. Link "
        "each from the skill or reference that needs it, by its path (a filename pattern links "
        f"nothing), or delete it. See {GUIDE_PATH}.\n"
        + "\n".join(f"  - {path}" for path in unreached)
    )
