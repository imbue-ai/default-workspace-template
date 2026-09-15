import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_behaviors.witnesses import find_broken_witness_links
from imbue.mngr_mapreduce.execution import GateResult
from imbue.mngr_mapreduce.execution import GateSubject
from imbue.mngr_mapreduce.interfaces import GateBindingInterface
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_witness.conventions import changelog_problems
from imbue.mngr_witness.conventions import is_changelog_path
from imbue.mngr_witness.conventions import is_test_path
from imbue.mngr_witness.gitops import BranchCommit
from imbue.mngr_witness.gitops import branch_commits
from imbue.mngr_witness.gitops import changed_paths
from imbue.mngr_witness.gitops import checked_out_worktree
from imbue.mngr_witness.gitops import file_text_at
from imbue.mngr_witness.outcomes import JunitOutcome
from imbue.mngr_witness.outcomes import OutcomeInvalidError
from imbue.mngr_witness.outcomes import OutcomeMissingError
from imbue.mngr_witness.outcomes import UnitRecord
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WitnessChangeKind
from imbue.mngr_witness.outcomes import load_mapper_outcome
from imbue.mngr_witness.outcomes import load_review_outcome
from imbue.mngr_witness.outcomes import read_junit_report
from imbue.mngr_witness.outcomes import read_witness_links
from imbue.mngr_witness.pipeline import MAP_NODE_NAME
from imbue.mngr_witness.pipeline import REVIEW_NODE_NAME

_TEST_ONLY_KINDS: Final[frozenset[str]] = frozenset(
    {
        WitnessChangeKind.SETUP.value,
        WitnessChangeKind.CREATE_TEST.value,
        WitnessChangeKind.IMPROVE_TEST.value,
        WitnessChangeKind.FIX_TEST.value,
        WitnessChangeKind.REVIEW.value,
        WitnessChangeKind.NORMALIZE.value,
    }
)
_RUFF_TIMEOUT_SECONDS: Final[float] = 300.0


@pure
def _passed(gate: Gate, detail: str) -> GateResult:
    return GateResult(gate_name=gate.name, is_passed=True, detail=detail)


@pure
def _failed(gate: Gate, detail: str) -> GateResult:
    return GateResult(gate_name=gate.name, is_passed=False, detail=detail)


def load_unit_records(subject: GateSubject) -> tuple[UnitRecord, ...] | None:
    """The unit records the subject's node publishes, or None for nodes that publish none (setup, reduce)."""
    if subject.node_name == MAP_NODE_NAME:
        return load_mapper_outcome(subject.archive_dir).units
    elif subject.node_name == REVIEW_NODE_NAME:
        return load_review_outcome(subject.archive_dir).units
    else:
        return None


@pure
def tree_ref(subject: GateSubject) -> str:
    """The ref whose tree the job left behind: its branch, or where it started when it committed nothing."""
    return subject.branch_name if subject.is_branch_applied else subject.job.base_ref


def subject_changed_paths(cg: ConcurrencyGroup, subject: GateSubject, under: Path | None) -> list[str]:
    """The paths the job's commits touch; a job that committed nothing touched none."""
    if not subject.is_branch_applied:
        return []
    return changed_paths(cg, subject.source_dir, subject.job.base_ref, subject.branch_name, under=under)


class CorpusUntouchedGate(GateBindingInterface):
    """Passes when the branch changes nothing under the corpus root."""

    corpus_root: Path = Field(frozen=True, description="The corpus root, relative to the repo root")
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        touched = subject_changed_paths(self.cg, subject, under=self.corpus_root)
        if touched:
            return _failed(gate, "The branch changes the corpus, which is read-only: " + ", ".join(touched))
        return _passed(gate, "The corpus is untouched")


class LintGate(GateBindingInterface):
    """Passes when ruff check and ruff format --check pass on the Python files the branch touches, at its tip."""

    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git and ruff")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        python_paths = [path for path in subject_changed_paths(self.cg, subject, under=None) if path.endswith(".py")]
        if not python_paths:
            return _passed(gate, "The branch touches no Python files")
        with checked_out_worktree(self.cg, subject.source_dir, subject.branch_name) as worktree:
            existing = [path for path in python_paths if (worktree / path).exists()]
            if not existing:
                return _passed(gate, "The branch only deletes Python files")
            problems = [
                *self._run_ruff(worktree, ["check", *existing]),
                *self._run_ruff(worktree, ["format", "--check", *existing]),
            ]
        if problems:
            return _failed(gate, "\n".join(problems))
        return _passed(gate, f"ruff is clean on {len(existing)} file(s)")

    def _run_ruff(self, worktree: Path, args: Sequence[str]) -> list[str]:
        result = self.cg.run_process_to_completion(
            [str(_ruff_executable()), *args], cwd=worktree, timeout=_RUFF_TIMEOUT_SECONDS, is_checked_after=False
        )
        if result.returncode == 0:
            return []
        return [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]


def _ruff_executable() -> Path:
    """The ruff of the environment running the pipeline, so the branch is checked with the repo's own ruff."""
    return Path(sys.executable).parent / "ruff"


class PathsGate(GateBindingInterface):
    """Passes when every commit touches only the paths its kind allows."""

    corpus_root: Path = Field(
        frozen=True, description="The corpus root, relative to the repo root; nothing may touch it"
    )
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        commits = (
            branch_commits(self.cg, subject.source_dir, subject.job.base_ref, subject.branch_name)
            if subject.is_branch_applied
            else []
        )
        violations = [
            violation for commit in commits for violation in commit_path_violations(commit, self.corpus_root)
        ]
        if violations:
            return _failed(gate, "\n".join(violations))
        return _passed(gate, f"All {len(commits)} commit(s) touch only the paths their kind allows")


@pure
def commit_path_violations(commit: BranchCommit, corpus_root: Path) -> list[str]:
    """Why a commit breaks the kind rule, one line per offending path, empty when it obeys."""
    short = commit.commit_hash[:10]
    if commit.kind is None:
        return [f"{short} has no recognized kind in its subject: {commit.subject!r}"]
    if commit.kind in _TEST_ONLY_KINDS:
        return [
            f"{short} [{commit.kind}] touches a non-test path: {path}"
            for path in commit.touched_paths
            if not is_test_path(path)
        ]
    elif commit.kind == WitnessChangeKind.CHANGELOG.value:
        return [
            f"{short} [{commit.kind}] touches a non-changelog path: {path}"
            for path in commit.touched_paths
            if not is_changelog_path(path)
        ]
    elif commit.kind == WitnessChangeKind.FIX_IMPL.value:
        return [
            f"{short} [{commit.kind}] touches the corpus: {path}"
            for path in commit.touched_paths
            if Path(path).is_relative_to(corpus_root)
        ]
    else:
        return [f"{short} has an unknown kind [{commit.kind}]"]


class NoChangelogGate(GateBindingInterface):
    """Passes when the branch adds or changes no changelog entry; the annealer alone writes those."""

    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        changelog_paths = [
            path for path in subject_changed_paths(self.cg, subject, under=None) if is_changelog_path(path)
        ]
        if changelog_paths:
            return _failed(
                gate,
                "The branch writes changelog entries, which only the reduce node may: " + ", ".join(changelog_paths),
            )
        return _passed(gate, "No changelog entry was written")


class ChangelogGate(GateBindingInterface):
    """Passes when the branch carries exactly one changelog entry per project it touches, named after the changelog branch."""

    changelog_branch: str = Field(
        frozen=True, description="The name every entry must carry, slashes replaced by dashes"
    )
    execution_base_commit: str = Field(
        frozen=True,
        description="The commit the execution started from; every project touched since then owes an entry",
    )
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        touched = changed_paths(self.cg, subject.source_dir, self.execution_base_commit, tree_ref(subject))
        problems = changelog_problems(touched, self.changelog_branch)
        if problems:
            return _failed(gate, "\n".join(problems))
        return _passed(gate, "Every touched project has exactly its changelog entry")


class UnitsReportedGate(GateBindingInterface):
    """Passes when the outcome reports exactly the units assigned to the job, no more and no fewer."""

    coordinates_by_slug: dict[str, tuple[str, ...]] = Field(
        frozen=True, description="The coordinates assigned to each job, by the job's slug"
    )

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        try:
            units = load_unit_records(subject)
        except (OutcomeMissingError, OutcomeInvalidError) as exc:
            return _failed(gate, str(exc))
        if units is None:
            return _passed(gate, "The node reports no units")
        reported = {unit.coordinate for unit in units}
        assigned = set(self.coordinates_by_slug.get(subject.job.slug, ()))
        missing = sorted(assigned - reported)
        unexpected = sorted(reported - assigned)
        if missing or unexpected:
            return _failed(gate, f"missing: {missing}; unexpected: {unexpected}")
        return _passed(gate, f"All {len(assigned)} assigned unit(s) are reported")


class LinksGate(GateBindingInterface):
    """Passes when the harvested witness links all name real units and every unit claimed witnessed has a link."""

    unit_coordinates: frozenset[str] = Field(frozen=True, description="Every coordinate the corpus defines")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        try:
            links = read_witness_links(subject.archive_dir)
            units = load_unit_records(subject)
        except (OutcomeMissingError, OutcomeInvalidError) as exc:
            return _failed(gate, str(exc))
        broken = find_broken_witness_links(links, self.unit_coordinates)
        if broken:
            return _failed(
                gate, "Broken witness links: " + ", ".join(f"{link.test} -> {link.coordinate!r}" for link in broken)
            )
        linked_coordinates = {link.coordinate for link in links}
        unlinked = [
            unit.coordinate
            for unit in units or ()
            if unit.verdict is not UnitVerdict.NONE and unit.coordinate not in linked_coordinates
        ]
        if unlinked:
            return _failed(gate, "Units claimed witnessed but linked by no test: " + ", ".join(unlinked))
        return _passed(gate, f"{len(links)} link(s), none broken")


class JunitGate(GateBindingInterface):
    """Passes when the shipped junit report has no failure or error and every traced test is present and passed."""

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        try:
            report = read_junit_report(subject.archive_dir)
            units = load_unit_records(subject)
        except (OutcomeMissingError, OutcomeInvalidError) as exc:
            return _failed(gate, str(exc))
        broken = [case.node_id for case in report.cases if case.outcome in (JunitOutcome.FAILED, JunitOutcome.ERROR)]
        if broken:
            return _failed(gate, "Failed or errored: " + ", ".join(broken))
        not_passed = [
            f"{test.node_id} ({report.outcome_of(test.node_id) or 'absent'})"
            for unit in units or ()
            for test in unit.tests
            if report.outcome_of(test.node_id) is not JunitOutcome.PASSED
        ]
        if not_passed:
            return _failed(gate, "Traced tests that did not pass: " + ", ".join(not_passed))
        return _passed(gate, f"{len(report.cases)} case(s) ran, none failed")


class TraceGate(GateBindingInterface):
    """Passes when every traced clause belongs to its unit, every traced assertion exists in its test, and verdicts match the trace."""

    clauses_by_coordinate: dict[str, tuple[str, ...]] = Field(
        frozen=True, description="Each unit's clauses, by coordinate"
    )
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")

    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        try:
            units = load_unit_records(subject)
        except (OutcomeMissingError, OutcomeInvalidError) as exc:
            return _failed(gate, str(exc))
        if units is None:
            return _passed(gate, "The node reports no units")
        problems: list[str] = []
        for unit in units:
            problems.extend(self._unit_trace_problems(unit, subject))
        if problems:
            return _failed(gate, "\n".join(problems))
        return _passed(gate, f"Traces of {len(units)} unit(s) are consistent")

    def _unit_trace_problems(self, unit: UnitRecord, subject: GateSubject) -> list[str]:
        clauses = set(self.clauses_by_coordinate.get(unit.coordinate, ()))
        problems: list[str] = []
        traced_clauses: set[str] = set()
        for test in unit.tests:
            source = file_text_at(self.cg, subject.source_dir, tree_ref(subject), test.node_id.split("::")[0])
            for entry in test.trace:
                if entry.clause not in clauses:
                    problems.append(f"{unit.coordinate}: traced clause is not one of the unit's: {entry.clause!r}")
                elif source is None or entry.assertion not in source:
                    problems.append(f"{unit.coordinate}: assertion not found in {test.node_id}: {entry.assertion!r}")
                else:
                    traced_clauses.add(entry.clause)
        problems.extend(verdict_trace_problems(unit, clauses, traced_clauses))
        return problems


@pure
def verdict_trace_problems(unit: UnitRecord, clauses: set[str], traced_clauses: set[str]) -> list[str]:
    """Why the unit's verdict disagrees with what its trace covers, empty when they agree."""
    open_world = set(unit.open_world_clauses)
    if unit.verdict is UnitVerdict.FULL:
        untraced = sorted(clauses - traced_clauses)
        problems = [f"{unit.coordinate}: FULL but clause untraced: {clause!r}" for clause in untraced]
        if open_world:
            problems.append(f"{unit.coordinate}: FULL but lists open-world clauses: {sorted(open_world)}")
        return problems
    elif unit.verdict is UnitVerdict.PARTIAL_STEADY:
        untraced = sorted(clauses - open_world - traced_clauses)
        problems = [
            f"{unit.coordinate}: PARTIAL_STEADY but closed-world clause untraced: {clause!r}" for clause in untraced
        ]
        if not open_world:
            problems.append(f"{unit.coordinate}: PARTIAL_STEADY but names no open-world clause")
        return problems
    else:
        return []


class GateSet(FrozenModel):
    """The nine gate bindings of the witness pipeline, keyed by the gate names the pipeline uses."""

    binding_by_gate_name: dict[str, GateBindingInterface] = Field(description="One binding per gate name")


def make_gate_set(
    corpus_root: Path,
    unit_coordinates: frozenset[str],
    clauses_by_coordinate: dict[str, tuple[str, ...]],
    coordinates_by_slug: dict[str, tuple[str, ...]],
    changelog_branch: str,
    execution_base_commit: str,
    cg: ConcurrencyGroup,
) -> GateSet:
    return GateSet(
        binding_by_gate_name={
            "corpus_untouched": CorpusUntouchedGate(corpus_root=corpus_root, cg=cg),
            "lint": LintGate(cg=cg),
            "paths": PathsGate(corpus_root=corpus_root, cg=cg),
            "no_changelog": NoChangelogGate(cg=cg),
            "changelog": ChangelogGate(
                changelog_branch=changelog_branch, execution_base_commit=execution_base_commit, cg=cg
            ),
            "units_reported": UnitsReportedGate(coordinates_by_slug=coordinates_by_slug),
            "links": LinksGate(unit_coordinates=unit_coordinates),
            "junit": JunitGate(),
            "trace": TraceGate(clauses_by_coordinate=clauses_by_coordinate, cg=cg),
        }
    )
