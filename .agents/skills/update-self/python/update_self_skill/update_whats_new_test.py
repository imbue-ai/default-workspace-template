"""Tests for what ``changelog-entries`` lists, against a template history that crosses the
switch from entry files to release sections in the root ``CHANGELOG.md``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from update_self_skill import update_self
from update_self_skill.update_whats_new import (
    CHANGELOG_PATH,
    ChangelogSection,
    list_whats_new,
    parse_changelog_sections,
    release_labels,
    select_unseen_sections,
)

_HEADER = "# Changelog - workspace template\n\nA summary of changes.\n\n"
_V1_SECTION = "## [minds-v1] - 2026-01-01\n\n- Changed: v1 thing.\n"
_V2_SECTION = (
    "## [minds-v2] - 2026-02-01\n\nSummary of v2.\n\n### Added\n\n- Added: v2 thing.\n"
)
_V3_SECTION = "## [minds-v3] - 2026-03-01\n\n- Fixed: v3 thing.\n"
_V4_SECTION = "## [minds-v4] - 2026-04-01\n\n- Added: v4 thing.\n"

# The last tag of the history below whose tree still holds its changes as entry files.
_LAST_ENTRY_TAG = "minds-v1"
_ENTRIES_ADDED_BY_V1 = [
    ".agents/changelog/third.md",
    "system/apps/chat/changelog/second.md",
]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _changelog(*release_sections: str, unreleased: str = "") -> str:
    return _HEADER + "\n".join([f"## [Unreleased]\n{unreleased}", *release_sections])


def _commit_and_tag(repo: Path, tag: str, files: dict[str, str | None]) -> None:
    """Write each file (or remove it, for ``None``), commit, and tag the commit."""
    for relative, text in files.items():
        path = repo / relative
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", tag)
    _git(repo, "tag", tag)


def _init_switched_template(tmp_path: Path) -> Path:
    """A template whose releases cross the switch to release sections.

    ``old`` holds entry files only, and ``rooted`` adds a ``CHANGELOG.md`` with no release
    section. ``minds-v1`` is the last release that keeps its changes as entry files:
    ``switch`` replaces them with a section for it. ``minds-v2`` and ``minds-v3`` each add
    their own section, and ``minds-v3`` also has one entry file not consolidated yet.
    """
    repo = tmp_path / "template"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit_and_tag(repo, "old", {"system/changelog/first.md": "First change.\n"})
    _commit_and_tag(repo, "rooted", {CHANGELOG_PATH: _changelog()})
    _commit_and_tag(
        repo,
        "minds-v1",
        {
            "system/apps/chat/changelog/second.md": "Second change.\n",
            ".agents/changelog/third.md": "Third change.\n",
            "system/vendor/tk/changelog/vendored.md": "Not the template's.\n",
        },
    )
    _commit_and_tag(
        repo,
        "switch",
        {
            "system/changelog/first.md": None,
            "system/apps/chat/changelog/second.md": None,
            ".agents/changelog/third.md": None,
            CHANGELOG_PATH: _changelog(_V1_SECTION),
        },
    )
    _commit_and_tag(
        repo, "minds-v2", {CHANGELOG_PATH: _changelog(_V2_SECTION, _V1_SECTION)}
    )
    _commit_and_tag(
        repo,
        "minds-v3",
        {
            CHANGELOG_PATH: _changelog(_V3_SECTION, _V2_SECTION, _V1_SECTION),
            "system/changelog/pending.md": "Pending change.\n",
        },
    )
    return repo


def _make_unreadable(repo: Path, ref: str, path: str) -> None:
    """Remove the object holding ``path`` at ``ref``, leaving the tree that names it."""
    sha = _git(repo, "rev-parse", f"{ref}:{path}")
    (repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()


def _truncate(repo: Path, ref: str, path: str) -> None:
    """Cut the object holding ``path`` at ``ref`` in half, so git dies partway through it."""
    sha = _git(repo, "rev-parse", f"{ref}:{path}")
    loose = repo / ".git" / "objects" / sha[:2] / sha[2:]
    data = loose.read_bytes()
    loose.unlink()
    loose.write_bytes(data[: len(data) // 2])


def test_sections_are_the_labelled_ones_each_ending_at_the_next_heading() -> None:
    text = _changelog(_V2_SECTION, "## Notes\n\nNot a release.\n", _V1_SECTION)

    assert parse_changelog_sections(text) == [
        ChangelogSection("Unreleased", "## [Unreleased]"),
        ChangelogSection("minds-v2", _V2_SECTION.rstrip()),
        ChangelogSection("minds-v1", _V1_SECTION.rstrip()),
    ]
    assert release_labels(parse_changelog_sections(text)) == ["minds-v2", "minds-v1"]
    assert parse_changelog_sections("") == []


@pytest.mark.parametrize(
    ("base_labels", "last_tag_with_entry_files", "expected"),
    [
        pytest.param(
            ["minds-v2", "minds-v1"], "minds-v1", ["minds-v3"], id="base-has-sections"
        ),
        pytest.param(
            [], "minds-v1", ["minds-v3", "minds-v2"], id="base-predates-sections"
        ),
        pytest.param(
            [],
            "minds-v0",
            ["minds-v3", "minds-v2", "minds-v1"],
            id="last-entry-tag-has-no-section",
        ),
        pytest.param(
            [], None, ["minds-v3", "minds-v2", "minds-v1"], id="no-last-entry-tag"
        ),
    ],
)
def test_unseen_sections_are_those_the_base_lacks_or_those_above_the_last_entry_tag(
    base_labels: list[str], last_tag_with_entry_files: str | None, expected: list[str]
) -> None:
    target_sections = parse_changelog_sections(
        _changelog(_V3_SECTION, _V2_SECTION, _V1_SECTION)
    )

    unseen = select_unseen_sections(
        target_sections, base_labels, last_tag_with_entry_files
    )

    assert [section.label for section in unseen] == expected


@pytest.mark.parametrize(
    "base_labels",
    [[], ["minds-v1"]],
    ids=["base-predates-sections", "base-has-sections"],
)
def test_unreleased_is_listed_only_when_it_holds_something(
    base_labels: list[str],
) -> None:
    pending = "\n### Added\n\n- Added: not released yet.\n"
    with_content = parse_changelog_sections(_changelog(_V1_SECTION, unreleased=pending))
    without_content = parse_changelog_sections(_changelog(_V1_SECTION))

    assert [
        section.label
        for section in select_unseen_sections(with_content, base_labels, "minds-v1")
    ] == ["Unreleased"]
    assert select_unseen_sections(without_content, base_labels, "minds-v1") == []


@pytest.mark.parametrize("base", ["old", "rooted"])
def test_refs_without_a_release_section_list_only_the_entries_added_between_them(
    tmp_path: Path, base: str
) -> None:
    repo = _init_switched_template(tmp_path)

    assert list_whats_new(base, "minds-v1", repo, None, None) == {
        "sections": [],
        "added": _ENTRIES_ADDED_BY_V1,
        "added_before_consolidation": None,
        "unlisted": [],
    }


@pytest.mark.parametrize(
    ("base", "target", "expected"),
    [
        pytest.param(
            "old",
            "minds-v2",
            {
                "sections": ["minds-v2"],
                "added": [],
                "added_before_consolidation": {
                    "ref": "minds-v1",
                    "paths": _ENTRIES_ADDED_BY_V1,
                },
                "unlisted": [],
            },
            id="older-than-the-switch-to-the-first-release-after-it",
        ),
        pytest.param(
            "old",
            "minds-v3",
            {
                "sections": ["minds-v3", "minds-v2"],
                "added": ["system/changelog/pending.md"],
                "added_before_consolidation": {
                    "ref": "minds-v1",
                    "paths": _ENTRIES_ADDED_BY_V1,
                },
                "unlisted": [],
            },
            id="older-than-the-switch-to-the-second-release-after-it",
        ),
        pytest.param(
            "rooted",
            "minds-v2",
            {
                "sections": ["minds-v2"],
                "added": [],
                "added_before_consolidation": {
                    "ref": "minds-v1",
                    "paths": _ENTRIES_ADDED_BY_V1,
                },
                "unlisted": [],
            },
            id="has-the-changelog-but-no-release-section",
        ),
        pytest.param(
            "minds-v1",
            "minds-v2",
            {
                "sections": ["minds-v2"],
                "added": [],
                "added_before_consolidation": {"ref": "minds-v1", "paths": []},
                "unlisted": [],
            },
            id="on-the-last-entry-tag",
        ),
        pytest.param(
            "minds-v2",
            "minds-v3",
            {
                "sections": ["minds-v3"],
                "added": ["system/changelog/pending.md"],
                "added_before_consolidation": None,
                "unlisted": [],
            },
            id="created-after-the-switch",
        ),
    ],
)
def test_an_update_across_the_switch_lists_unseen_sections_and_the_entries_of_skipped_releases(
    tmp_path: Path, base: str, target: str, expected: dict[str, object]
) -> None:
    repo = _init_switched_template(tmp_path)

    assert list_whats_new(base, target, repo, None, _LAST_ENTRY_TAG) == expected
    # Git runs wherever the caller is; the changelog is still the one at the repository root.
    assert (
        list_whats_new(base, target, repo / "system", None, _LAST_ENTRY_TAG) == expected
    )


def test_out_holds_the_sections_then_each_entry_as_it_is_at_the_ref_that_has_it(
    tmp_path: Path,
) -> None:
    repo = _init_switched_template(tmp_path)
    out = tmp_path / "scratch" / "whats-new.md"

    list_whats_new("old", "minds-v3", repo, out, _LAST_ENTRY_TAG)

    assert out.read_text() == (
        "# Release sections of CHANGELOG.md new to this workspace\n\n"
        f"{_V3_SECTION}\n{_V2_SECTION}\n"
        "# Changelog entries the update adds, as they are at minds-v3\n\n"
        "## system/changelog/pending.md\n\nPending change.\n\n"
        "# Changelog entries the update adds, as they are at minds-v1\n\n"
        "## .agents/changelog/third.md\n\nThird change.\n\n"
        "## system/apps/chat/changelog/second.md\n\nSecond change.\n"
    )


def test_out_is_empty_when_the_update_brings_no_changelog_text(tmp_path: Path) -> None:
    repo = _init_switched_template(tmp_path)
    out = tmp_path / "whats-new.md"

    list_whats_new("minds-v2", "minds-v2", repo, out, _LAST_ENTRY_TAG)

    assert out.read_text() == ""


def test_an_entry_identical_to_one_the_update_deletes_is_still_listed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "template"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    text = "".join(f"A line of the entry, number {index}.\n" for index in range(20))
    _commit_and_tag(repo, "base", {"system/changelog/consolidated.md": text})
    _commit_and_tag(
        repo,
        "target",
        {"system/changelog/consolidated.md": None, "system/changelog/renamed.md": text},
    )

    added = list_whats_new("base", "target", repo, None, None)["added"]

    assert added == ["system/changelog/renamed.md"]


def test_a_missing_last_entry_tag_leaves_out_only_its_entries(tmp_path: Path) -> None:
    repo = _init_switched_template(tmp_path)
    _git(repo, "tag", "-d", _LAST_ENTRY_TAG)

    answer = list_whats_new("old", "minds-v3", repo, None, _LAST_ENTRY_TAG)

    unlisted = answer.pop("unlisted")
    assert answer == {
        "sections": ["minds-v3", "minds-v2"],
        "added": ["system/changelog/pending.md"],
        "added_before_consolidation": None,
    }
    assert isinstance(unlisted, list) and len(unlisted) == 1
    assert unlisted[0].startswith(
        "the changelog entries at minds-v1 could not be listed: "
    )


def test_an_unreadable_base_changelog_lists_every_section_above_the_last_entry_tag(
    tmp_path: Path,
) -> None:
    repo = _init_switched_template(tmp_path)
    _make_unreadable(repo, "minds-v2", CHANGELOG_PATH)

    answer = list_whats_new("minds-v2", "minds-v3", repo, None, _LAST_ENTRY_TAG)

    unlisted = answer.pop("unlisted")
    assert answer == {
        "sections": ["minds-v3", "minds-v2"],
        "added": ["system/changelog/pending.md"],
        "added_before_consolidation": None,
    }
    assert isinstance(unlisted, list) and len(unlisted) == 1
    assert unlisted[0].startswith("CHANGELOG.md at minds-v2 could not be read")


def test_an_unreadable_target_changelog_still_lists_the_entries(tmp_path: Path) -> None:
    repo = _init_switched_template(tmp_path)
    _make_unreadable(repo, "minds-v3", CHANGELOG_PATH)

    answer = list_whats_new("old", "minds-v3", repo, None, _LAST_ENTRY_TAG)

    unlisted = answer.pop("unlisted")
    assert answer == {
        "sections": [],
        "added": ["system/changelog/pending.md"],
        "added_before_consolidation": {
            "ref": "minds-v1",
            "paths": _ENTRIES_ADDED_BY_V1,
        },
    }
    assert isinstance(unlisted, list) and len(unlisted) == 1
    assert unlisted[0].startswith("CHANGELOG.md at minds-v3 could not be read")


@pytest.mark.parametrize(
    ("v4_changelog", "expected_sections", "problem"),
    [
        pytest.param(
            _changelog(_V4_SECTION.replace("[minds-v4]", "minds-v4"), _V3_SECTION),
            ["minds-v3"],
            "has headings that are not '## [<label>]' ones: '## minds-v4 - 2026-04-01'",
            id="unbracketed-heading",
        ),
        pytest.param(
            _changelog(_V4_SECTION, _V3_SECTION, _V3_SECTION),
            ["minds-v4", "minds-v3", "minds-v3"],
            "has more than one section for [minds-v3]",
            id="repeated-label",
        ),
    ],
)
def test_a_target_changelog_with_a_misplaced_heading_names_it(
    tmp_path: Path, v4_changelog: str, expected_sections: list[str], problem: str
) -> None:
    repo = _init_switched_template(tmp_path)
    _commit_and_tag(repo, "minds-v4", {CHANGELOG_PATH: v4_changelog})

    answer = list_whats_new("minds-v2", "minds-v4", repo, None, _LAST_ENTRY_TAG)

    assert answer["sections"] == expected_sections
    assert answer["unlisted"] == [
        f"CHANGELOG.md at minds-v4 {problem}, so the sections listed may be incomplete"
    ]


def test_a_base_changelog_with_no_bracketed_release_heading_reads_no_earlier_release(
    tmp_path: Path,
) -> None:
    repo = _init_switched_template(tmp_path)
    unbracketed = _V3_SECTION.replace("[minds-v3]", "minds-v3")
    _commit_and_tag(repo, "bad-base", {CHANGELOG_PATH: _changelog(unbracketed)})
    _commit_and_tag(
        repo, "minds-v4", {CHANGELOG_PATH: _changelog(_V4_SECTION, _V3_SECTION)}
    )

    answer = list_whats_new("bad-base", "minds-v4", repo, None, _LAST_ENTRY_TAG)

    assert answer == {
        "sections": ["minds-v4", "minds-v3"],
        "added": [],
        "added_before_consolidation": None,
        "unlisted": [
            "CHANGELOG.md at bad-base could not be read, so release sections the "
            "workspace already has may be listed again and no entry is read at an "
            "earlier release: it has headings that are not '## [<label>]' ones: "
            "'## minds-v3 - 2026-03-01'"
        ],
    }


def test_out_leaves_out_an_entry_that_cannot_be_read_and_names_it(
    tmp_path: Path,
) -> None:
    repo = _init_switched_template(tmp_path)
    _make_unreadable(repo, _LAST_ENTRY_TAG, ".agents/changelog/third.md")
    out = tmp_path / "whats-new.md"

    answer = list_whats_new("old", "minds-v2", repo, out, _LAST_ENTRY_TAG)

    assert answer["unlisted"] == [
        ".agents/changelog/third.md at minds-v1 could not be read"
    ]
    assert out.read_text() == (
        "# Release sections of CHANGELOG.md new to this workspace\n\n"
        f"{_V2_SECTION}\n"
        "# Changelog entries the update adds, as they are at minds-v1\n\n"
        "## system/apps/chat/changelog/second.md\n\nSecond change.\n"
    )


def test_out_reads_no_entry_from_a_batch_that_git_cuts_short(tmp_path: Path) -> None:
    repo = _init_switched_template(tmp_path)
    _truncate(repo, _LAST_ENTRY_TAG, _ENTRIES_ADDED_BY_V1[0])
    out = tmp_path / "whats-new.md"

    answer = list_whats_new("old", "minds-v2", repo, out, _LAST_ENTRY_TAG)

    assert answer["unlisted"] == [
        f"{path} at minds-v1 could not be read" for path in _ENTRIES_ADDED_BY_V1
    ]
    assert out.read_text() == (
        "# Release sections of CHANGELOG.md new to this workspace\n\n"
        f"{_V2_SECTION}\n"
        "# Changelog entries the update adds, as they are at minds-v1\n"
    )


def test_the_command_prints_the_listing_and_writes_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_switched_template(tmp_path)
    out = tmp_path / "whats-new.md"

    code = update_self.main(
        [
            "changelog-entries",
            "--base",
            "minds-v2",
            "--target",
            "minds-v3",
            "--out",
            str(out),
            "--repo-root",
            str(repo),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "sections": ["minds-v3"],
        "added": ["system/changelog/pending.md"],
        "added_before_consolidation": None,
        "unlisted": [],
    }
    assert out.read_text() == (
        "# Release sections of CHANGELOG.md new to this workspace\n\n"
        f"{_V3_SECTION}\n"
        "# Changelog entries the update adds, as they are at minds-v3\n\n"
        "## system/changelog/pending.md\n\nPending change.\n"
    )


def test_the_command_survives_a_changelog_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_switched_template(tmp_path)
    _make_unreadable(repo, "minds-v3", CHANGELOG_PATH)

    code = update_self.main(
        [
            "changelog-entries",
            "--base",
            "minds-v2",
            "--target",
            "minds-v3",
            "--repo-root",
            str(repo),
        ]
    )

    assert code == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["sections"] == []
    assert answer["added"] == ["system/changelog/pending.md"]
    assert len(answer["unlisted"]) == 1
