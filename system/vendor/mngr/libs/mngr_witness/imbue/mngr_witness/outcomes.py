import json
from enum import auto
from pathlib import Path
from typing import Final
from typing import TypeVar
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_behaviors.data_types import WitnessLink

# The layout of every stage agent's outputs archive. The publish snippet copies the
# agent's ``.test_output`` directory into the archive as ``test_output``.
ARCHIVE_TEST_OUTPUT_DIRNAME: Final[str] = "test_output"
OUTCOME_FILENAME: Final[str] = "outcome.json"
WITNESS_LINKS_FILENAME: Final[str] = "witness_links.jsonl"
JUNIT_FILENAME: Final[str] = "junit.xml"
SCAFFOLDING_GUIDE_FILENAME: Final[str] = "scaffolding_guide.md"


class OutcomeMissingError(MngrError, FileNotFoundError):
    """Raised when an archive lacks the file a stage was required to publish."""

    ...


class OutcomeInvalidError(MngrError, ValueError):
    """Raised when a published outcome or evidence file does not match its schema."""

    ...


class WitnessChangeKind(UpperCaseStrEnum):
    """The kinds of commit a stage agent may make; each commit's message starts with the kind in brackets."""

    SETUP = auto()
    CREATE_TEST = auto()
    IMPROVE_TEST = auto()
    FIX_TEST = auto()
    FIX_IMPL = auto()
    REVIEW = auto()
    NORMALIZE = auto()
    CHANGELOG = auto()


class ChangeStatus(UpperCaseStrEnum):
    """How an attempted change ended."""

    SUCCEEDED = auto()
    FAILED = auto()
    SKIPPED = auto()


class Change(FrozenModel):
    """One change an agent attempted, and the commit it landed in when it succeeded."""

    kind: WitnessChangeKind = Field(description="The kind of change, matching the commit message prefix")
    status: ChangeStatus = Field(description="How the attempt ended")
    commit_hash: str | None = Field(description="The commit the change landed in, or None when it did not land")
    summary: str = Field(description="What was changed, or why it was not")


class UnitVerdict(UpperCaseStrEnum):
    """The end state claimed for one behavior unit; FULL is defined positively, by clauses witnessed."""

    NONE = auto()
    PARTIAL_IMPROVABLE = auto()
    PARTIAL_STEADY = auto()
    FULL = auto()


class TraceEntry(FrozenModel):
    """One assertion in a witnessing test, named against the clause it witnesses."""

    clause: str = Field(description="The clause as written in the feature file: a Then/And/But step, or a Rule's name")
    assertion: str = Field(description="The assertion as written in the test; it must occur in the test's source")


class WitnessedTest(FrozenModel):
    """One test that witnesses a unit, with its partial note and its clause trace."""

    node_id: str = Field(description="The pytest node id of the test")
    partial: str | None = Field(description="The marker's partial= note, or None when the test covers the unit fully")
    trace: tuple[TraceEntry, ...] = Field(description="Which assertion witnesses which clause")


class BehaviorProblem(FrozenModel):
    """A behavior-seems-wrong escalation: the only channel by which an agent proposes a corpus change."""

    problem: str = Field(description="What looks wrong about the behavior unit")
    proposed_edit: str = Field(description="The corpus edit the agent would propose; never applied by the pipeline")


class UnitRecord(FrozenModel):
    """A mapper's or reviewer's claim about one behavior unit: its verdict and the tests and clauses behind it."""

    coordinate: str = Field(description="The behavior unit's coordinate")
    verdict: UnitVerdict = Field(description="The claimed end state of the unit")
    tests: tuple[WitnessedTest, ...] = Field(description="The tests that witness the unit, with their traces")
    open_world_clauses: tuple[str, ...] = Field(
        description="Clauses no test of any kind can close; the only residue that earns PARTIAL_STEADY"
    )
    blockers: tuple[str, ...] = Field(description="Shared-setup or environment needs that blocked the unit")
    behavior_problems: tuple[BehaviorProblem, ...] = Field(description="Proposed corpus edits, never applied")
    summary_markdown: str = Field(description="What happened for this unit, for the report")


class ScaffoldingKind(UpperCaseStrEnum):
    """What a piece of shared test scaffolding is."""

    FIXTURE = auto()
    HELPER = auto()
    FACTORY = auto()


class ScaffoldingEntry(FrozenModel):
    """One fixture, helper, or factory the corpus's tests share, and when a witnessing test should use it."""

    name: str = Field(description="The fixture, helper, or factory name as imported or requested")
    kind: ScaffoldingKind = Field(description="What it is")
    location: str = Field(description="The file that defines it")
    provides: str = Field(description="What it gives a test")
    use_when: str = Field(description="Which kinds of scenario should use it")


class SetupOutcome(FrozenModel):
    """What the setup stage found and made: the scaffolding every mapper is told to use."""

    scaffolding: tuple[ScaffoldingEntry, ...] = Field(description="Every fixture, helper, and factory found or added")
    changes: tuple[Change, ...] = Field(description="The setup commit, when anything was added")
    summary_markdown: str = Field(description="What the stage did, for the report")


class MapperOutcome(FrozenModel):
    """What a mapper claims for its feature file's units, and the commits it made."""

    units: tuple[UnitRecord, ...] = Field(description="One record per unit assigned to the mapper, always all of them")
    changes: tuple[Change, ...] = Field(description="Every change attempted, one per kind used")
    scaffolding_added: tuple[ScaffoldingEntry, ...] = Field(
        description="Scaffolding the mapper had to add because the guide named none for the need"
    )
    errored: bool = Field(description="Whether an infrastructure error prevented the mapper from working")
    summary_markdown: str = Field(description="What the mapper did, for the report")


class FindingKind(UpperCaseStrEnum):
    """What a reviewer found wrong with a mapper's work."""

    GOLD_PLATING = auto()
    MISSING_CLAUSE = auto()
    UNTRUTHFUL_PARTIAL = auto()
    DUPLICATE_COVERAGE = auto()
    OVER_PRECISE = auto()
    EVIDENCE_MISMATCH = auto()
    IMPLEMENTATION_DIVERGENCE = auto()


class Finding(FrozenModel):
    """One thing a reviewer found, and the commit that fixed it when it could be fixed."""

    coordinate: str = Field(description="The unit the finding concerns")
    kind: FindingKind = Field(description="What kind of problem it is")
    detail: str = Field(description="What was found")
    resolved_in_commit: str | None = Field(description="The reviewer's commit that fixed it, or None")


class ReviewOutcome(FrozenModel):
    """A reviewer's verdicts, which replace the mapper's, and what it found and fixed."""

    units: tuple[UnitRecord, ...] = Field(description="One record per unit of the reviewed job, always all of them")
    findings: tuple[Finding, ...] = Field(description="Everything found, fixed or not")
    branch_accepted: bool = Field(description="Whether the reviewed branch is sound enough to integrate")
    changes: tuple[Change, ...] = Field(description="The reviewer's commits")
    summary_markdown: str = Field(description="What the reviewer did, for the report")


class Conflict(FrozenModel):
    """A cherry-pick the orchestrator could not complete, and how the annealer resolved it."""

    branch: str = Field(description="The branch whose commit conflicted")
    commit: str = Field(description="The conflicting commit")
    paths: tuple[str, ...] = Field(description="The paths that conflicted")
    resolution: str = Field(description="How the annealer resolved it")


class Normalization(FrozenModel):
    """One collapse of duplicated scaffolding or one parametrization the annealer made."""

    detail: str = Field(description="What was collapsed or parametrized")
    commit: str | None = Field(description="The commit it landed in, or None when it was not applied")


class Escalation(FrozenModel):
    """Something the annealer could not resolve and hands to the operator."""

    detail: str = Field(description="What needs a human")
    coordinates: tuple[str, ...] = Field(description="The units it concerns")


class ReduceOutcome(FrozenModel):
    """What the annealer integrated, normalized, and escalated."""

    integrated_branches: tuple[str, ...] = Field(description="Every branch whose commits are in the integrated branch")
    conflicts_resolved: tuple[Conflict, ...] = Field(description="Cherry-picks the annealer had to finish by hand")
    normalizations: tuple[Normalization, ...] = Field(description="Duplicates collapsed and families parametrized")
    escalations: tuple[Escalation, ...] = Field(description="What still needs a human")
    changelog_paths: tuple[str, ...] = Field(description="The changelog entries written, one per touched project")
    changes: tuple[Change, ...] = Field(description="The annealer's commits")
    summary_markdown: str = Field(description="What the annealer did, for the report")


class JunitOutcome(UpperCaseStrEnum):
    """How one test case ended in a junit report."""

    PASSED = auto()
    FAILED = auto()
    ERROR = auto()
    SKIPPED = auto()


class JunitCase(FrozenModel):
    """One test case from a junit report, identified by its pytest node id."""

    node_id: str = Field(description="``<file>::<name>``, as pytest identifies the test")
    outcome: JunitOutcome = Field(description="How the case ended")


class JunitReport(FrozenModel):
    """Every test case a stage agent ran, as evidence."""

    cases: tuple[JunitCase, ...] = Field(description="One entry per test case, in report order")

    def outcome_of(self, node_id: str) -> JunitOutcome | None:
        """The outcome of the named test, or None when it did not run."""
        for case in self.cases:
            if case.node_id == node_id:
                return case.outcome
        return None


TOutcome = TypeVar("TOutcome", bound=FrozenModel)


def _archive_file(archive_dir: Path, filename: str) -> Path:
    path = archive_dir / ARCHIVE_TEST_OUTPUT_DIRNAME / filename
    if not path.exists():
        raise OutcomeMissingError(f"The archive at {archive_dir} has no {ARCHIVE_TEST_OUTPUT_DIRNAME}/{filename}")
    return path


def load_mapper_outcome(archive_dir: Path) -> MapperOutcome:
    return _load_outcome(archive_dir, MapperOutcome)


def load_review_outcome(archive_dir: Path) -> ReviewOutcome:
    return _load_outcome(archive_dir, ReviewOutcome)


def _load_outcome(archive_dir: Path, model: type[TOutcome]) -> TOutcome:
    """Read and validate the stage's outcome file; a missing or malformed file is an error, never a default."""
    path = _archive_file(archive_dir, OUTCOME_FILENAME)
    try:
        return model.model_validate_json(path.read_text())
    except ValidationError as exc:
        raise OutcomeInvalidError(f"{path} is not a valid {model.__name__}: {exc}") from exc


def read_witness_links(archive_dir: Path) -> tuple[WitnessLink, ...]:
    """Read the links an agent harvested with the witness collection plugin, one JSON object per line."""
    path = _archive_file(archive_dir, WITNESS_LINKS_FILENAME)
    links: list[WitnessLink] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            links.append(WitnessLink.model_validate_json(line))
        except ValidationError as exc:
            raise OutcomeInvalidError(f"{path}:{line_number} is not a witness link: {exc}") from exc
    return tuple(links)


def read_junit_report(archive_dir: Path) -> JunitReport:
    """Read the junit XML an agent shipped; pytest's xunit1 layout carries the file and test name per case."""
    path = _archive_file(archive_dir, JUNIT_FILENAME)
    try:
        return parse_junit_report(path.read_text())
    except (ValueError, OutcomeInvalidError) as exc:
        raise OutcomeInvalidError(f"{path} is not a junit report: {exc}") from exc


def parse_junit_report(xml_text: str) -> JunitReport:
    """Turn junit XML into cases keyed by pytest node id; raises OutcomeInvalidError when a case lacks a file."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise OutcomeInvalidError(f"malformed XML: {exc}") from exc
    cases: list[JunitCase] = []
    for element in root.iter("testcase"):
        cases.append(JunitCase(node_id=_junit_case_node_id(element), outcome=_junit_case_outcome(element)))
    return JunitReport(cases=tuple(cases))


def _junit_case_node_id(element: Element) -> str:
    """The pytest node id of a case: the name when a conftest already wrote the full id there, else file::name."""
    name = element.get("name")
    if name is None:
        raise OutcomeInvalidError("a testcase lacks the name attribute")
    if "::" in name:
        return name
    file_attribute = element.get("file")
    if file_attribute is None:
        raise OutcomeInvalidError("a testcase lacks the file attribute; run pytest with junit_family=xunit1")
    return f"{file_attribute}::{name}"


def _junit_case_outcome(element: Element) -> JunitOutcome:
    if element.find("failure") is not None:
        return JunitOutcome.FAILED
    elif element.find("error") is not None:
        return JunitOutcome.ERROR
    elif element.find("skipped") is not None:
        return JunitOutcome.SKIPPED
    else:
        return JunitOutcome.PASSED


def write_outcome_json(outcome: FrozenModel) -> str:
    """The JSON an agent must write; kept here so prompts and tests render the schema the loaders accept."""
    return json.dumps(json.loads(outcome.model_dump_json()), indent=2)
