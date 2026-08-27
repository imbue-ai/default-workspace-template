from versioning.data_types import ChangeStats
from versioning.data_types import VersionKind
from versioning.git_repo import parse_numstat_log
from versioning.magnitude import DOT_MAX_DIAMETER_PX
from versioning.magnitude import DOT_MIN_DIAMETER_PX
from versioning.magnitude import MagnitudeTier
from versioning.magnitude import change_magnitude
from versioning.magnitude import change_tier
from versioning.magnitude import dot_diameter_px
from versioning.magnitude import size_adjective
from versioning.magnitude import version_phrase


def test_magnitude_counts_lines_and_weights_files() -> None:
    assert change_magnitude(ChangeStats(files_changed=2, lines_changed=100)) == 150.0


def test_all_five_tiers_have_distinct_dot_sizes_in_ascending_order() -> None:
    sizes = [
        dot_diameter_px(ChangeStats(files_changed=0, lines_changed=lines))
        for lines in (5, 100, 500, 1500, 5000)
    ]
    assert sizes[0] == DOT_MIN_DIAMETER_PX
    assert sizes[-1] == DOT_MAX_DIAMETER_PX
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == 5


def test_dot_size_and_adjective_always_derive_from_the_same_tier() -> None:
    cases = [
        ChangeStats(files_changed=0, lines_changed=0),
        ChangeStats(files_changed=1, lines_changed=100),
        ChangeStats(files_changed=2, lines_changed=149),
        ChangeStats(files_changed=5, lines_changed=500),
        ChangeStats(files_changed=8, lines_changed=1800),
        ChangeStats(files_changed=50, lines_changed=20_000),
    ]
    diameter_by_tier = {}
    adjective_by_tier = {}
    for stats in cases:
        tier = change_tier(stats)
        diameter_by_tier.setdefault(tier, dot_diameter_px(stats))
        adjective_by_tier.setdefault(tier, size_adjective(stats))
        assert dot_diameter_px(stats) == diameter_by_tier[tier]
        assert size_adjective(stats) == adjective_by_tier[tier]


def test_version_phrase_folds_size_and_kind_into_one_noun_phrase() -> None:
    small = ChangeStats(files_changed=1, lines_changed=100)
    sweeping = ChangeStats(files_changed=50, lines_changed=20_000)

    assert version_phrase(VersionKind.FIX, small) == "A small fix"
    assert version_phrase(VersionKind.CHANGE, sweeping) == "A sweeping update"
    assert version_phrase(VersionKind.HARDEN, small) == "A small background tidy-up"
    # Commits from before the trailer convention record no kind at all.
    assert version_phrase(None, small) == "A small change"


def test_version_phrase_is_absent_where_the_versions_own_name_already_says_it() -> None:
    small = ChangeStats(files_changed=1, lines_changed=100)

    # A restore is named "Restored from ...", so a phrase under it would repeat it.
    assert version_phrase(VersionKind.RESTORE, small) is None
    # An unmeasurable diff has no size to report.
    assert version_phrase(VersionKind.FIX, None) is None
    # These two say what they are without needing a size.
    assert version_phrase(VersionKind.BUILD, small) == "The first build"
    assert version_phrase(VersionKind.PORT, small) == "Brought back from an earlier version"


def test_tier_boundaries() -> None:
    assert change_tier(ChangeStats(files_changed=0, lines_changed=59)) == MagnitudeTier.TINY
    assert change_tier(ChangeStats(files_changed=0, lines_changed=60)) == MagnitudeTier.SMALL
    assert change_tier(ChangeStats(files_changed=0, lines_changed=249)) == MagnitudeTier.SMALL
    assert change_tier(ChangeStats(files_changed=0, lines_changed=250)) == MagnitudeTier.MODERATE
    assert change_tier(ChangeStats(files_changed=0, lines_changed=899)) == MagnitudeTier.MODERATE
    assert change_tier(ChangeStats(files_changed=0, lines_changed=900)) == MagnitudeTier.BIG
    assert change_tier(ChangeStats(files_changed=0, lines_changed=2499)) == MagnitudeTier.BIG
    assert change_tier(ChangeStats(files_changed=0, lines_changed=2500)) == MagnitudeTier.SWEEPING


def test_parse_numstat_log_attributes_lines_to_the_right_commit() -> None:
    sha_a = "a" * 40
    sha_b = "b" * 40
    output = f"{sha_a}\n\n10\t2\tsystem/apps/demo/one.py\n3\t0\tsystem/apps/demo/two.py\n\n{sha_b}\n\n1\t1\tsystem/apps/demo/one.py\n"
    stats = parse_numstat_log(output)
    assert stats[sha_a] == ChangeStats(files_changed=2, lines_changed=15)
    assert stats[sha_b] == ChangeStats(files_changed=1, lines_changed=2)


def test_parse_numstat_log_weighs_binary_files() -> None:
    sha = "c" * 40
    output = f"{sha}\n\n-\t-\tsystem/apps/demo/logo.png\n5\t0\tsystem/apps/demo/app.py\n"
    stats = parse_numstat_log(output)
    assert stats[sha].files_changed == 2
    assert stats[sha].lines_changed == 45


def test_parse_numstat_log_handles_commit_with_no_changes() -> None:
    sha = "d" * 40
    assert parse_numstat_log(f"{sha}\n") == {sha: ChangeStats(files_changed=0, lines_changed=0)}
