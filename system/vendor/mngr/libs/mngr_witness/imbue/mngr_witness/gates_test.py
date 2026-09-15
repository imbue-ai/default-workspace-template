from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr_behaviors.testing import write_behavior_corpus
from imbue.mngr_mapreduce.execution import GateSubject
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.interfaces import GateBindingInterface
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.conventions import changelog_problems
from imbue.mngr_witness.conventions import is_test_path
from imbue.mngr_witness.conventions import project_of_path
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.corpus import discover_feature_tasks
from imbue.mngr_witness.corpus import scan_valid_corpus
from imbue.mngr_witness.gates import ChangelogGate
from imbue.mngr_witness.gates import CorpusUntouchedGate
from imbue.mngr_witness.gates import JunitGate
from imbue.mngr_witness.gates import LinksGate
from imbue.mngr_witness.gates import LintGate
from imbue.mngr_witness.gates import NoChangelogGate
from imbue.mngr_witness.gates import PathsGate
from imbue.mngr_witness.gates import TraceGate
from imbue.mngr_witness.gates import UnitsReportedGate
from imbue.mngr_witness.gates import make_gate_set
from imbue.mngr_witness.gates import verdict_trace_problems
from imbue.mngr_witness.outcomes import ARCHIVE_TEST_OUTPUT_DIRNAME
from imbue.mngr_witness.outcomes import Change
from imbue.mngr_witness.outcomes import ChangeStatus
from imbue.mngr_witness.outcomes import JUNIT_FILENAME
from imbue.mngr_witness.outcomes import MapperOutcome
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import TraceEntry
from imbue.mngr_witness.outcomes import UnitRecord
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.outcomes import WitnessChangeKind
from imbue.mngr_witness.outcomes import WitnessedTest
from imbue.mngr_witness.outcomes import write_outcome_json
from imbue.mngr_witness.pipeline import MAP_NODE_NAME
from imbue.mngr_witness.pipeline import REDUCE_NODE_NAME

_CORPUS_ROOT = Path("libs/proj/behaviors")
_COORDINATE = "area.thing"
_CLAUSE = "Then the thing answers ok"
_TEST_PATH = "libs/proj/imbue/proj/witnesses/area/thing_test.py"
_NODE_ID = f"{_TEST_PATH}::test_thing_answers_ok"
_ASSERTION = 'assert answer() == "ok"'
_FEATURE = f"""Feature: The thing

  @thing
  Scenario: The thing answers
    When the thing is asked
    {_CLAUSE}
"""
_TEST_SOURCE = f"""import pytest

from imbue.proj.thing import answer


@pytest.mark.witnesses("{_COORDINATE}")
def test_thing_answers_ok() -> None:
    {_ASSERTION}
"""


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def _commit(cg: ConcurrencyGroup, repo: Path, subject: str, files: dict[str, str]) -> str:
    for relative_path, text in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(cg, repo, "add", relative_path)
    _git(cg, repo, "commit", "--quiet", "-m", subject)
    return _git(cg, repo, "rev-parse", "HEAD")


class _Repo:
    """A temp repo with a one-unit corpus and an implementation, plus helpers to branch off its base commit."""

    def __init__(self, repo: Path, cg: ConcurrencyGroup) -> None:
        self.repo = repo
        self.cg = cg
        write_behavior_corpus(repo / _CORPUS_ROOT, {"area/thing.feature": _FEATURE})
        self.base = _commit(
            cg,
            repo,
            "base",
            {
                "libs/proj/imbue/proj/__init__.py": "",
                "libs/proj/imbue/proj/thing.py": 'def answer() -> str:\n    return "ok"\n',
                "libs/proj/behaviors/.keep": "",
            },
        )
        _git(cg, repo, "add", str(_CORPUS_ROOT))
        _git(cg, repo, "commit", "--quiet", "--allow-empty", "-m", "corpus")
        self.base = _git(cg, repo, "rev-parse", "HEAD")

    def branch(self, commits: list[tuple[str, dict[str, str]]]) -> str:
        name = f"witness/{uuid4().hex}"
        _git(self.cg, self.repo, "checkout", "--quiet", "-b", name, self.base)
        for subject, files in commits:
            _commit(self.cg, self.repo, subject, files)
        _git(self.cg, self.repo, "checkout", "--quiet", self.base)
        return name

    def witness_branch(self) -> str:
        return self.branch([(f"[CREATE_TEST] {_COORDINATE}: witness it", {_TEST_PATH: _TEST_SOURCE})])


@pytest.fixture
def repo(temp_git_repo: Path, cg: ConcurrencyGroup) -> _Repo:
    return _Repo(temp_git_repo, cg)


def _unit(verdict: UnitVerdict, trace: tuple[TraceEntry, ...], open_world: tuple[str, ...] = ()) -> UnitRecord:
    return UnitRecord(
        coordinate=_COORDINATE,
        verdict=verdict,
        tests=(WitnessedTest(node_id=_NODE_ID, partial=None, trace=trace),),
        open_world_clauses=open_world,
        blockers=(),
        behavior_problems=(),
        summary_markdown="",
    )


_GOOD_UNIT = _unit(UnitVerdict.FULL, (TraceEntry(clause=_CLAUSE, assertion=_ASSERTION),))


def _write_archive(
    archive_dir: Path,
    units: tuple[UnitRecord, ...] = (_GOOD_UNIT,),
    junit_outcome_tag: str = "",
    junit_node_id: str = _NODE_ID,
    link_coordinate: str = _COORDINATE,
) -> Path:
    test_output = archive_dir / ARCHIVE_TEST_OUTPUT_DIRNAME
    test_output.mkdir(parents=True, exist_ok=True)
    outcome = MapperOutcome(
        units=units,
        changes=(
            Change(kind=WitnessChangeKind.CREATE_TEST, status=ChangeStatus.SUCCEEDED, commit_hash="abc", summary=""),
        ),
        scaffolding_added=(),
        errored=False,
        summary_markdown="",
    )
    (test_output / OUTCOME_FILENAME).write_text(write_outcome_json(outcome))
    file_path, name = junit_node_id.split("::")
    (test_output / JUNIT_FILENAME).write_text(
        f'<testsuites><testsuite><testcase classname="c" name="{name}" file="{file_path}">{junit_outcome_tag}</testcase></testsuite></testsuites>'
    )
    (test_output / WITNESS_LINKS_FILENAME).write_text(
        f'{{"test": "{_NODE_ID}", "coordinate": "{link_coordinate}", "partial": null}}\n'
    )
    return archive_dir


def _subject(
    repo: _Repo,
    branch: str,
    archive_dir: Path,
    node_name: NodeName = MAP_NODE_NAME,
    slug: str = "area-thing",
    is_branch_applied: bool = True,
) -> GateSubject:
    return GateSubject(
        node_name=node_name,
        job=Job(slug=slug, base_ref=repo.base, template_variables={}, inputs_dir=None),
        branch_name=branch,
        is_branch_applied=is_branch_applied,
        archive_dir=archive_dir,
        source_dir=repo.repo,
    )


def _gate(name: str) -> Gate:
    return Gate(name=NonEmptyStr(name), description="")


def _gates(repo: _Repo, changelog_branch: str = "witness/run/integrated") -> dict[str, GateBindingInterface]:
    corpus_root = repo.repo / _CORPUS_ROOT
    scan = scan_valid_corpus(corpus_root)
    tasks = discover_feature_tasks(
        scan, corpus_root, UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=None)
    )
    return make_gate_set(
        corpus_root=_CORPUS_ROOT,
        unit_coordinates=frozenset(unit.coordinate for unit in scan.units),
        clauses_by_coordinate={unit.coordinate: unit.clauses for task in tasks for unit in task.units},
        coordinates_by_slug={task.slug: task.coordinates for task in tasks},
        changelog_branch=changelog_branch,
        execution_base_commit=repo.base,
        cg=repo.cg,
    ).binding_by_gate_name


def test_every_gate_passes_on_a_well_formed_witness_branch(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    subject = _subject(repo, branch, _write_archive(tmp_path / "archive"))
    gates = _gates(repo)

    results = {
        name: gates[name].check(_gate(name), subject)
        for name in ("corpus_untouched", "lint", "paths", "no_changelog", "units_reported", "links", "junit", "trace")
    }

    assert {name: result.is_passed for name, result in results.items()} == dict.fromkeys(results, True), results


def test_corpus_untouched_fails_when_the_branch_edits_the_corpus(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.branch(
        [(f"[FIX_IMPL] {_COORDINATE}: edit corpus", {f"{_CORPUS_ROOT}/area/thing.feature": _FEATURE + "\n"})]
    )
    gate = CorpusUntouchedGate(corpus_root=_CORPUS_ROOT, cg=repo.cg)

    result = gate.check(_gate("corpus_untouched"), _subject(repo, branch, _write_archive(tmp_path / "a")))

    assert not result.is_passed
    assert "thing.feature" in result.detail


def test_lint_fails_on_an_unused_import_at_the_branch_tip(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.branch([(f"[CREATE_TEST] {_COORDINATE}: sloppy", {_TEST_PATH: "import os\n" + _TEST_SOURCE})])

    result = LintGate(cg=repo.cg).check(_gate("lint"), _subject(repo, branch, _write_archive(tmp_path / "a")))

    assert not result.is_passed
    assert "F401" in result.detail


@pytest.mark.parametrize(
    ("subject_line", "path", "is_expected_passed"),
    [
        ("[CREATE_TEST] a: ok", _TEST_PATH, True),
        ("[CREATE_TEST] a: touches impl", "libs/proj/imbue/proj/thing.py", False),
        ("[FIX_IMPL] a: impl is fine", "libs/proj/imbue/proj/thing.py", True),
        ("[FIX_IMPL] a: corpus is not", f"{_CORPUS_ROOT}/area/thing.feature", False),
        ("[CHANGELOG] entry", "libs/proj/changelog/witness-run.md", True),
        ("[CHANGELOG] not an entry", _TEST_PATH, False),
        ("no kind at all", _TEST_PATH, False),
        ("[MYSTERY] kind", _TEST_PATH, False),
    ],
)
def test_paths_gate_holds_each_commit_to_its_kind(
    repo: _Repo, tmp_path: Path, subject_line: str, path: str, is_expected_passed: bool
) -> None:
    branch = repo.branch([(subject_line, {path: "changed\n"})])

    result = PathsGate(corpus_root=_CORPUS_ROOT, cg=repo.cg).check(
        _gate("paths"), _subject(repo, branch, _write_archive(tmp_path / "a"))
    )

    assert result.is_passed is is_expected_passed, result.detail


def test_no_changelog_fails_when_a_mapper_writes_an_entry(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.branch([("[CREATE_TEST] a: with entry", {"libs/proj/changelog/oops.md": "entry\n"})])

    result = NoChangelogGate(cg=repo.cg).check(
        _gate("no_changelog"), _subject(repo, branch, _write_archive(tmp_path / "a"))
    )

    assert not result.is_passed
    assert "oops.md" in result.detail


def test_changelog_gate_wants_exactly_one_entry_per_touched_project(repo: _Repo, tmp_path: Path) -> None:
    complete = repo.branch(
        [
            (f"[CREATE_TEST] {_COORDINATE}: witness", {_TEST_PATH: _TEST_SOURCE}),
            ("[CHANGELOG] entries", {"libs/proj/changelog/witness-run-integrated.md": "Add witnesses.\n"}),
        ]
    )
    incomplete = repo.branch([(f"[CREATE_TEST] {_COORDINATE}: witness", {_TEST_PATH: _TEST_SOURCE})])
    gate = ChangelogGate(changelog_branch="witness/run/integrated", execution_base_commit=repo.base, cg=repo.cg)

    passed = gate.check(
        _gate("changelog"), _subject(repo, complete, _write_archive(tmp_path / "a"), node_name=REDUCE_NODE_NAME)
    )
    failed = gate.check(
        _gate("changelog"), _subject(repo, incomplete, _write_archive(tmp_path / "b"), node_name=REDUCE_NODE_NAME)
    )

    assert passed.is_passed, passed.detail
    assert not failed.is_passed
    assert "missing changelog entry libs/proj/changelog/witness-run-integrated.md" in failed.detail


def test_changelog_problems_names_missing_and_unexpected_entries() -> None:
    touched = ["libs/a/x.py", "apps/b/y.py", "scripts/z.py", "libs/a/changelog/feat.md", "libs/c/changelog/feat.md"]

    assert changelog_problems(touched, "feat") == [
        "missing changelog entry apps/b/changelog/feat.md",
        "missing changelog entry dev/changelog/feat.md",
        "unexpected changelog entry libs/c/changelog/feat.md",
    ]


def test_units_reported_fails_on_a_missing_or_extra_unit(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    extra = _unit(UnitVerdict.NONE, ()).model_copy_update(to_update(_GOOD_UNIT.field_ref().coordinate, "area.other"))
    archive = _write_archive(tmp_path / "a", units=(_GOOD_UNIT, extra))
    gate = UnitsReportedGate(coordinates_by_slug={"area-thing": (_COORDINATE,)})

    result = gate.check(_gate("units_reported"), _subject(repo, branch, archive))

    assert not result.is_passed
    assert "unexpected: ['area.other']" in result.detail


def test_links_fails_on_a_broken_link_and_on_a_witnessed_unit_without_one(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    gate = LinksGate(unit_coordinates=frozenset({_COORDINATE}))

    broken = gate.check(
        _gate("links"), _subject(repo, branch, _write_archive(tmp_path / "a", link_coordinate="area.nowhere"))
    )
    unlinked_unit = _unit(UnitVerdict.FULL, ()).model_copy_update(
        to_update(_GOOD_UNIT.field_ref().coordinate, "area.other")
    )
    unlinked = gate.check(
        _gate("links"),
        _subject(repo, branch, _write_archive(tmp_path / "b", units=(_GOOD_UNIT, unlinked_unit))),
    )

    assert not broken.is_passed and "area.nowhere" in broken.detail
    assert not unlinked.is_passed and "area.other" in unlinked.detail


def test_junit_fails_on_a_failure_and_on_a_traced_test_that_did_not_run(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    gate = JunitGate()

    failed = gate.check(
        _gate("junit"),
        _subject(repo, branch, _write_archive(tmp_path / "a", junit_outcome_tag="<failure>x</failure>")),
    )
    absent = gate.check(
        _gate("junit"),
        _subject(repo, branch, _write_archive(tmp_path / "b", junit_node_id=f"{_TEST_PATH}::test_other")),
    )

    assert not failed.is_passed and _NODE_ID in failed.detail
    assert not absent.is_passed and "absent" in absent.detail


def test_trace_fails_when_a_clause_is_foreign_or_an_assertion_is_not_in_the_test(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    gate = TraceGate(clauses_by_coordinate={_COORDINATE: (_CLAUSE,)}, cg=repo.cg)

    foreign = gate.check(
        _gate("trace"),
        _subject(
            repo,
            branch,
            _write_archive(
                tmp_path / "a",
                units=(_unit(UnitVerdict.FULL, (TraceEntry(clause="Then pigs fly", assertion=_ASSERTION),)),),
            ),
        ),
    )
    invented = gate.check(
        _gate("trace"),
        _subject(
            repo,
            branch,
            _write_archive(
                tmp_path / "b",
                units=(_unit(UnitVerdict.FULL, (TraceEntry(clause=_CLAUSE, assertion="assert 2 + 2 == 5"),)),),
            ),
        ),
    )

    assert not foreign.is_passed and "not one of the unit's" in foreign.detail
    assert not invented.is_passed and "assertion not found" in invented.detail


def test_verdict_trace_problems_hold_full_and_steady_to_their_definitions() -> None:
    clauses = {"Then a", "And b"}

    assert verdict_trace_problems(_unit(UnitVerdict.FULL, ()), clauses, {"Then a"}) == [
        f"{_COORDINATE}: FULL but clause untraced: 'And b'"
    ]
    assert verdict_trace_problems(_unit(UnitVerdict.FULL, (), open_world=("And b",)), clauses, clauses) == [
        f"{_COORDINATE}: FULL but lists open-world clauses: ['And b']"
    ]
    assert (
        verdict_trace_problems(_unit(UnitVerdict.PARTIAL_STEADY, (), open_world=("And b",)), clauses, {"Then a"}) == []
    )
    assert verdict_trace_problems(_unit(UnitVerdict.PARTIAL_STEADY, ()), clauses, {"Then a"}) == [
        f"{_COORDINATE}: PARTIAL_STEADY but closed-world clause untraced: 'And b'",
        f"{_COORDINATE}: PARTIAL_STEADY but names no open-world clause",
    ]
    assert verdict_trace_problems(_unit(UnitVerdict.PARTIAL_IMPROVABLE, ()), clauses, set()) == []


@pytest.mark.parametrize(
    ("path", "is_expected_test"),
    [
        ("libs/p/imbue/p/thing_test.py", True),
        ("libs/p/imbue/p/test_thing.py", True),
        ("libs/p/imbue/p/conftest.py", True),
        ("libs/p/testing.py", True),
        ("libs/p/imbue/p/witnesses/__init__.py", True),
        ("libs/p/imbue/p/testing/helpers.py", True),
        ("libs/p/imbue/p/thing.py", False),
        ("libs/p/imbue/p/tests.py", False),
    ],
)
def test_is_test_path(path: str, is_expected_test: bool) -> None:
    assert is_test_path(path) is is_expected_test


@pytest.mark.parametrize(
    ("path", "expected_project"),
    [
        ("libs/mngr/x.py", "libs/mngr"),
        ("apps/minds/y.py", "apps/minds"),
        ("scripts/z.py", "dev"),
        ("pyproject.toml", "dev"),
        ("libs/x", "dev"),
    ],
)
def test_project_of_path(path: str, expected_project: str) -> None:
    assert project_of_path(path) == expected_project


def test_gates_that_read_the_archive_fail_when_it_is_missing_its_files(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    empty_archive = tmp_path / "empty"
    empty_archive.mkdir()
    subject = _subject(repo, branch, empty_archive)
    gates = _gates(repo)

    results = {name: gates[name].check(_gate(name), subject) for name in ("units_reported", "links", "junit", "trace")}

    assert {name: result.is_passed for name, result in results.items()} == dict.fromkeys(results, False)
    assert all("has no test_output/" in result.detail for result in results.values()), results


def test_unit_gates_pass_trivially_for_stages_that_report_no_units(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.witness_branch()
    subject = _subject(repo, branch, _write_archive(tmp_path / "a"), node_name=REDUCE_NODE_NAME)
    gates = _gates(repo)

    assert gates["units_reported"].check(_gate("units_reported"), subject).is_passed
    assert gates["trace"].check(_gate("trace"), subject).is_passed
    assert gates["links"].check(_gate("links"), subject).is_passed


def test_lint_passes_a_branch_that_only_deletes_python_files(repo: _Repo, tmp_path: Path) -> None:
    branch = repo.branch([])
    _git(repo.cg, repo.repo, "checkout", "--quiet", branch)
    _git(repo.cg, repo.repo, "rm", "--quiet", "libs/proj/imbue/proj/thing.py")
    _git(repo.cg, repo.repo, "commit", "--quiet", "-m", "[FIX_IMPL] delete the thing")
    _git(repo.cg, repo.repo, "checkout", "--quiet", repo.base)

    result = LintGate(cg=repo.cg).check(_gate("lint"), _subject(repo, branch, _write_archive(tmp_path / "a")))

    assert result.is_passed
    assert "only deletes" in result.detail
