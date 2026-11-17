"""Every skill the workspace holds to its checks passes ``validate_skill``, and every file
under ``.agents/shared/`` is reached from something.

The rules and the reasons for them are in ``docs/system/skill_authoring_guide.md``. A skill
listed in ``.agents/third-party-skills.toml`` is kept as it came and is not checked.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from agents_shared.skill_checks import (
    GUIDE_PATH,
    SkillTree,
    skill_dirs,
    unreached_shared_files,
)
from agents_shared.validate_skill import third_party_skills, validate
from update_self_skill import update_self

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


def test_every_reference_the_update_flow_reaches_is_one_it_stages() -> None:
    """A reference update-self's prose reaches, through any number of links, is staged.

    After its hand-off the flow reads the target release's copy of each file, and a staged
    file sends its reader on to others. One that ``stage-shared-prose`` does not stage would
    be read from the workspace's own tree, which may be older than the flow following it.
    Another skill's SKILL.md is a skill to invoke, not prose the flow reads on from.
    """
    tree = SkillTree(_REPO_ROOT)
    skill = update_self.SKILL_DIR_REL
    staged = tuple(f"{path}/" for path in (skill, *update_self.STAGED_PROSE_PATHS))
    own = [name for name in tree.files_under(skill) if name.endswith(".md")]
    reached = set(own)
    queue = deque(own)
    while queue:
        for linked in tree.links(queue.popleft(), skill):
            is_reference = (
                linked.startswith(".agents/")
                and linked.endswith(".md")
                and not linked.endswith("/SKILL.md")
            )
            if is_reference and linked not in reached:
                reached.add(linked)
                if linked.startswith(staged):
                    queue.append(linked)
    # The files this guard exists for; without them it is vacuous.
    assert ".agents/shared/references/lead-proxy.md" in reached
    assert any(name.startswith(".agents/skills/harden-worker/") for name in reached)

    assert sorted(name for name in reached if not name.startswith(staged)) == []
