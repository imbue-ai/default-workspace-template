"""Tests for the skill authoring checks, their exemptions, and the stamping mode."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents_shared import validate_skill
from agents_shared.skill_checks import (
    MAX_SKILL_MD_BYTES,
    Check,
    check_failures,
    unreached_shared_files,
)
from agents_shared.skill_exemptions import ExemptionReason

_LONG_FILLER = "\n".join(f"filler line {index}" for index in range(120))


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(
    root: Path, name: str, body: str = "Do the thing.\n", metadata: str = ""
) -> Path:
    """Write ``<root>/.agents/skills/<name>/SKILL.md``; return the skill's directory."""
    frontmatter = f"---\nname: {name}\ndescription: Does the thing.\n{metadata}---\n"
    return _write(root, f".agents/skills/{name}/SKILL.md", frontmatter + body).parent


def _failed(skill: Path) -> set[Check]:
    return {failure.check for failure in check_failures(skill)}


def _exemption(checks: str, reason: str = "predates-checks") -> str:
    return f"metadata:\n  exempt-checks: {checks}\n  exempt-reason: {reason}\n"


def test_skill_md_at_the_byte_cap_passes_and_one_byte_over_fails(
    tmp_path: Path,
) -> None:
    skill = _skill(tmp_path, "sized")
    skill_md = skill / "SKILL.md"
    padding = MAX_SKILL_MD_BYTES - len(skill_md.read_bytes())
    skill_md.write_text(skill_md.read_text() + "x" * padding)
    assert _failed(skill) == set()

    skill_md.write_text(skill_md.read_text() + "x")
    assert _failed(skill) == {Check.SIZE}


def test_an_unlinked_file_is_unreached_until_skill_md_links_it(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "reach")
    _write(skill, "references/extra.md", "More.\n")
    _write(skill, "scripts/fetch.sh", "#!/bin/sh\n")
    assert _failed(skill) == {Check.REACHABILITY}

    _skill(
        tmp_path, "reach", "Read `references/extra.md`, then run `scripts/fetch.sh`.\n"
    )
    assert _failed(skill) == set()


def test_a_python_project_is_reached_by_naming_one_of_its_files(tmp_path: Path) -> None:
    skill = _skill(
        tmp_path,
        "packaged",
        "Run `.agents/skills/packaged/python/packaged_skill/run.py`.\n",
    )
    _write(skill, "python/pyproject.toml", "[project]\nname = 'packaged-skill'\n")
    _write(skill, "python/packaged_skill/run.py", "")
    _write(skill, "python/packaged_skill/helpers.py", "")
    _write(skill, "python/packaged_skill/run_test.py", "")
    assert _failed(skill) == set()


def test_a_filename_pattern_links_nothing(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "pattern", "Read `references/op-<operation>.md`.\n")
    _write(skill, "references/op-heal.md", "Heal.\n")
    assert _failed(skill) == {Check.REACHABILITY}


def test_a_three_link_chain_is_too_deep_until_skill_md_links_its_end(
    tmp_path: Path,
) -> None:
    chain = "Read `references/one.md`.\n"
    skill = _skill(tmp_path, "deep", chain)
    _write(skill, "references/one.md", "Read `.agents/shared/references/two.md`.\n")
    _write(tmp_path, ".agents/shared/references/two.md", "Read `three.md`.\n")
    _write(tmp_path, ".agents/shared/references/three.md", "The end.\n")
    failures = check_failures(skill)
    assert [failure.check for failure in failures] == [Check.DEPTH]
    assert "three.md is 3 links away" in failures[0].detail

    _skill(tmp_path, "deep", chain + "Then `.agents/shared/references/three.md`.\n")
    assert _failed(skill) == set()


def test_another_skills_file_ends_a_link_instead_of_being_read_on_from(
    tmp_path: Path,
) -> None:
    other = _skill(tmp_path, "other", "Read `references/far.md`.\n")
    _write(other, "references/far.md", "Read `references/farther.md`.\n")
    _write(other, "references/farther.md", "Read `system/missing.py`.\n")
    skill = _skill(tmp_path, "near", "See `.agents/skills/other/references/far.md`.\n")
    assert _failed(skill) == set()


def test_a_missing_tracked_path_fails_and_untracked_kinds_are_skipped(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "system/scripts/real.py", "")
    skipped = (
        "Run `system/scripts/real.py` over `data/.apps/<name>/rows.json`, "
        "`system/apps/<name>/app.toml`, `system/apps/*/app.toml`, `$WT/system/gone.py`, "
        "`/home/user/workspace/system/gone.py` and `<mngr>/docs/gone.md`.\n"
    )
    skill = _skill(tmp_path, "pathy", skipped)
    assert _failed(skill) == set()

    _skill(tmp_path, "pathy", skipped + "Then `system/scripts/gone.py`.\n")
    failures = check_failures(skill)
    assert [failure.check for failure in failures] == [Check.PATHS]
    assert "system/scripts/gone.py" in failures[0].detail


def test_in_a_checkout_an_ignored_path_is_skipped_whether_or_not_it_is_on_disk(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, ".gitignore", "system/generated/\n")
    skill = _skill(
        tmp_path,
        "built",
        "Read `system/generated/out.js` and `system/generated/absent.js`.\n",
    )
    _write(tmp_path, "system/generated/out.js", "")
    _write(skill, "references/ignored-here.md", "Not a file git would track.\n")
    _write(skill, ".gitignore", "references/ignored-here.md\n")
    assert _failed(skill) == set()


@pytest.mark.parametrize(
    ("opening", "expected"),
    [
        pytest.param("Intro.\n\n", {Check.CONTENTS}, id="no-list"),
        pytest.param(
            "Intro.\n\n- First part\n\n", {Check.CONTENTS}, id="list-omits-a-heading"
        ),
        pytest.param(
            "Intro.\n\n- First part\n- Second part\n\n", set(), id="plain-list"
        ),
        pytest.param(
            "## Steps\n\n```\n- [ ] 1. First part (ends the turn)\n- [ ] 2. second PART\n```\n\n",
            set(),
            id="fenced-checklist-under-its-own-heading",
        ),
        pytest.param(
            "- [the first](#first-part)\n- [the second](#second-part)\n\n",
            set(),
            id="anchor-links",
        ),
    ],
)
def test_a_long_reference_needs_a_list_naming_every_section_below_it(
    tmp_path: Path, opening: str, expected: set[Check]
) -> None:
    skill = _skill(tmp_path, "long", "Read `references/long.md`.\n")
    sections = f"## First part\n\n{_LONG_FILLER}\n\n## Second part\n\n```\n## Not a section\n```\n"
    _write(skill, "references/long.md", f"# Long\n\n{opening}{sections}")
    assert _failed(skill) == expected


def test_a_contents_list_past_the_opening_does_not_count(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "late", "Read `references/late.md`.\n")
    _write(
        skill,
        "references/late.md",
        f"# Late\n\n{_LONG_FILLER}\n\n- First part\n\n## First part\n\nText.\n",
    )
    assert _failed(skill) == {Check.CONTENTS}


def test_a_short_reference_and_a_skill_md_under_its_own_threshold_need_no_list(
    tmp_path: Path,
) -> None:
    body = "Read `references/short.md`.\n\n## One\n\n" + _LONG_FILLER + "\n"
    skill = _skill(tmp_path, "short", body)
    _write(skill, "references/short.md", "## Only\n\nText.\n")
    assert _failed(skill) == set()


def test_an_exemption_excuses_only_the_checks_it_names(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "excused", "Read `system/gone.py`.\n", _exemption("paths"))
    assert validate_skill.validate(skill) is None

    _write(skill, "references/orphan.md", "Unlinked.\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "[reachability]" in error
    assert "[paths]" not in error
    assert "Restructuring the skill is permitted" in error
    assert "exempt" not in error


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        pytest.param(
            _exemption("size paths"),
            "names size, which the skill now passes",
            id="stale",
        ),
        pytest.param(
            _exemption("paths tidiness"), "must name one or more of", id="unknown-check"
        ),
        pytest.param(
            _exemption("paths", "because"), "must be one of", id="unknown-reason"
        ),
        pytest.param(
            "metadata:\n  exempt-checks: paths\n", "together", id="reason-missing"
        ),
    ],
)
def test_a_stale_or_malformed_exemption_fails(
    tmp_path: Path, metadata: str, expected: str
) -> None:
    skill = _skill(tmp_path, "wrong", "Read `system/gone.py`.\n", metadata)
    error = validate_skill.validate(skill)
    assert error is not None
    assert expected in error


def test_no_exemptions_mode_refuses_a_valid_exemption(tmp_path: Path) -> None:
    passing = _skill(tmp_path, "edited", "Fine now.\n", _exemption("paths"))
    failing = _skill(
        tmp_path, "failing", "Read `system/gone.py`.\n", _exemption("paths")
    )
    passing_error = validate_skill.validate(passing, allow_exemptions=False)
    failing_error = validate_skill.validate(failing, allow_exemptions=False)
    assert passing_error is not None and "passes every check" in passing_error
    assert failing_error is not None and "[paths]" in failing_error


def test_stamping_adds_missing_exemptions_drops_stale_ones_and_skips_third_parties(
    tmp_path: Path,
) -> None:
    failing = _skill(
        tmp_path, "failing", "Read `system/gone.py`.\n", "metadata:\n  author: me\n"
    )
    fixed = _skill(
        tmp_path, "fixed", "Fine now.\n", _exemption("paths", "awaiting-restructure")
    )
    narrower = _skill(
        tmp_path,
        "narrower",
        "Read `system/gone.py`.\n",
        _exemption("size paths", "awaiting-restructure"),
    )
    passing = _skill(tmp_path, "passing")
    third_party = _skill(tmp_path, "vendored", "Read `system/gone.py`.\n")
    _write(
        tmp_path,
        ".agents/third-party-skills.toml",
        '[skills]\nvendored = "https://example.com"\n',
    )
    untouched = {
        skill: (skill / "SKILL.md").read_text() for skill in (passing, third_party)
    }
    failing_body = (failing / "SKILL.md").read_text().split("---\n", 2)[2]

    changes = validate_skill.stamp_exemptions(tmp_path, ExemptionReason.PREDATES_CHECKS)

    assert changes == [
        "failing: exempt-checks none -> paths (predates-checks)",
        "fixed: exempt-checks paths -> none",
        "narrower: exempt-checks size paths -> paths (awaiting-restructure)",
    ]
    assert (failing / "SKILL.md").read_text() == (
        "---\nname: failing\ndescription: Does the thing.\nmetadata:\n  author: me\n"
        "  exempt-checks: paths\n  exempt-reason: predates-checks\n---\n" + failing_body
    )
    assert "metadata" not in (fixed / "SKILL.md").read_text()
    for skill, text in untouched.items():
        assert (skill / "SKILL.md").read_text() == text
    for skill in (failing, fixed, narrower, passing):
        assert validate_skill.validate(skill) is None
    assert (
        validate_skill.stamp_exemptions(tmp_path, ExemptionReason.PREDATES_CHECKS) == []
    )


def test_stamping_settles_when_the_exemption_itself_tips_skill_md_over_the_size_cap(
    tmp_path: Path,
) -> None:
    skill = _skill(tmp_path, "brimming", "Read `system/gone.py`.\n")
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text() + "x" * (MAX_SKILL_MD_BYTES - len(skill_md.read_bytes()))
    )

    validate_skill.stamp_exemptions(tmp_path, ExemptionReason.PREDATES_CHECKS)

    assert "exempt-checks: size paths\n" in skill_md.read_text()
    assert validate_skill.validate(skill) is None


def test_stamping_drops_a_size_exemption_that_only_its_own_lines_keep_true(
    tmp_path: Path,
) -> None:
    skill = _skill(tmp_path, "borderline", metadata=_exemption("size"))
    skill_md = skill / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text()
        + "x" * (MAX_SKILL_MD_BYTES + 1 - len(skill_md.read_bytes()))
    )
    assert validate_skill.validate(skill) is None

    changes = validate_skill.stamp_exemptions(tmp_path, ExemptionReason.PREDATES_CHECKS)

    assert changes == ["borderline: exempt-checks size -> none"]
    assert validate_skill.validate(skill) is None


def test_stamping_replaces_an_exemption_it_cannot_read_whether_or_not_the_skill_fails(
    tmp_path: Path,
) -> None:
    half_written = _skill(
        tmp_path, "half-written", metadata="metadata:\n  exempt-checks: paths\n"
    )
    unknown_check = _skill(tmp_path, "unknown-check", metadata=_exemption("tidiness"))
    unknown_reason = _skill(
        tmp_path,
        "unknown-reason",
        "Read `system/gone.py`.\n",
        _exemption("paths", "because"),
    )

    changes = validate_skill.stamp_exemptions(tmp_path, ExemptionReason.PREDATES_CHECKS)

    assert changes == [
        "half-written: exempt-checks unreadable -> none",
        "unknown-check: exempt-checks unreadable -> none",
        "unknown-reason: exempt-checks unreadable -> paths (predates-checks)",
    ]
    for skill in (half_written, unknown_check, unknown_reason):
        assert validate_skill.validate(skill) is None


def test_a_shared_file_is_reached_from_a_skill_agents_md_or_a_program_and_reported_otherwise(
    tmp_path: Path,
) -> None:
    _skill(tmp_path, "user", "Read `.agents/shared/references/from-skill.md`.\n")
    _write(tmp_path, ".agents/shared/references/from-skill.md", "Read `onward.md`.\n")
    _write(tmp_path, ".agents/shared/references/onward.md", "The end.\n")
    _write(
        tmp_path, "AGENTS.md", "See `.agents/shared/references/from-agents-md.md`.\n"
    )
    _write(tmp_path, ".agents/shared/references/from-agents-md.md", "Text.\n")
    _write(
        tmp_path,
        "system/apps/chat/handoff.py",
        'TEMPLATE = ".agents/shared/references/template.md"\n',
    )
    _write(tmp_path, ".agents/shared/references/template.md", "Text.\n")
    _write(tmp_path, ".agents/shared/references/orphan.md", "Text.\n")
    _write(
        tmp_path,
        ".agents/shared/scripts/pyproject.toml",
        "[project]\nname = 'shared'\n",
    )
    _write(tmp_path, ".agents/shared/scripts/shared/unnamed.py", "")

    assert unreached_shared_files(tmp_path) == [
        ".agents/shared/references/orphan.md",
        ".agents/shared/scripts",
    ]
