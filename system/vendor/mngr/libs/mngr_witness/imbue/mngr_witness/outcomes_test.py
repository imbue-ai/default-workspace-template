import json
from pathlib import Path

import pytest

from imbue.mngr_witness.outcomes import ARCHIVE_TEST_OUTPUT_DIRNAME
from imbue.mngr_witness.outcomes import Change
from imbue.mngr_witness.outcomes import ChangeStatus
from imbue.mngr_witness.outcomes import JUNIT_FILENAME
from imbue.mngr_witness.outcomes import JunitOutcome
from imbue.mngr_witness.outcomes import MapperOutcome
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import OutcomeInvalidError
from imbue.mngr_witness.outcomes import OutcomeMissingError
from imbue.mngr_witness.outcomes import TraceEntry
from imbue.mngr_witness.outcomes import UnitRecord
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.outcomes import WitnessChangeKind
from imbue.mngr_witness.outcomes import WitnessedTest
from imbue.mngr_witness.outcomes import load_mapper_outcome
from imbue.mngr_witness.outcomes import parse_junit_report
from imbue.mngr_witness.outcomes import read_junit_report
from imbue.mngr_witness.outcomes import read_witness_links
from imbue.mngr_witness.outcomes import write_outcome_json

_JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4">
    <testcase classname="pkg.server_test" name="test_passes" file="pkg/server_test.py" line="10" time="0.01"/>
    <testcase classname="pkg.server_test" name="test_fails" file="pkg/server_test.py" line="20" time="0.01">
      <failure message="assert 1 == 2">boom</failure>
    </testcase>
    <testcase classname="pkg.server_test" name="test_errors" file="pkg/server_test.py" line="30" time="0.01">
      <error message="fixture blew up">bang</error>
    </testcase>
    <testcase classname="pkg.server_test" name="test_skipped[x]" file="pkg/server_test.py" line="40" time="0.0">
      <skipped message="needs docker"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _mapper_outcome() -> MapperOutcome:
    return MapperOutcome(
        units=(
            UnitRecord(
                coordinate="authentication.fresh-code",
                verdict=UnitVerdict.FULL,
                tests=(
                    WitnessedTest(
                        node_id="pkg/server_test.py::test_fresh_code",
                        partial=None,
                        trace=(TraceEntry(clause='Then the browser lands on "/"', assertion='assert loc == "/"'),),
                    ),
                ),
                open_world_clauses=(),
                blockers=(),
                behavior_problems=(),
                summary_markdown="witnessed",
            ),
        ),
        changes=(
            Change(
                kind=WitnessChangeKind.CREATE_TEST,
                status=ChangeStatus.SUCCEEDED,
                commit_hash="abc123",
                summary="one test",
            ),
        ),
        scaffolding_added=(),
        errored=False,
        summary_markdown="done",
    )


def _write_archive_file(archive_dir: Path, filename: str, text: str) -> None:
    test_output = archive_dir / ARCHIVE_TEST_OUTPUT_DIRNAME
    test_output.mkdir(parents=True, exist_ok=True)
    (test_output / filename).write_text(text)


def test_mapper_outcome_round_trips_through_the_json_an_agent_writes(tmp_path: Path) -> None:
    outcome = _mapper_outcome()
    _write_archive_file(tmp_path, OUTCOME_FILENAME, write_outcome_json(outcome))

    assert load_mapper_outcome(tmp_path) == outcome


def test_a_missing_outcome_file_is_an_error_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(OutcomeMissingError, match=OUTCOME_FILENAME):
        load_mapper_outcome(tmp_path)


def test_an_outcome_with_an_unknown_verdict_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(write_outcome_json(_mapper_outcome()))
    raw["units"][0]["verdict"] = "MOSTLY"
    _write_archive_file(tmp_path, OUTCOME_FILENAME, json.dumps(raw))

    with pytest.raises(OutcomeInvalidError, match="MapperOutcome"):
        load_mapper_outcome(tmp_path)


def test_junit_cases_are_keyed_by_node_id_with_their_outcome() -> None:
    report = parse_junit_report(_JUNIT_XML)

    assert [(case.node_id, case.outcome) for case in report.cases] == [
        ("pkg/server_test.py::test_passes", JunitOutcome.PASSED),
        ("pkg/server_test.py::test_fails", JunitOutcome.FAILED),
        ("pkg/server_test.py::test_errors", JunitOutcome.ERROR),
        ("pkg/server_test.py::test_skipped[x]", JunitOutcome.SKIPPED),
    ]
    assert report.outcome_of("pkg/server_test.py::test_passes") is JunitOutcome.PASSED
    assert report.outcome_of("pkg/server_test.py::test_never_ran") is None


def test_junit_without_file_attributes_is_rejected_with_the_fix_named(tmp_path: Path) -> None:
    _write_archive_file(tmp_path, JUNIT_FILENAME, '<testsuite><testcase classname="a" name="t"/></testsuite>')

    with pytest.raises(OutcomeInvalidError, match="xunit1"):
        read_junit_report(tmp_path)


def test_malformed_junit_is_rejected(tmp_path: Path) -> None:
    _write_archive_file(tmp_path, JUNIT_FILENAME, "<testsuite><testcase")

    with pytest.raises(OutcomeInvalidError, match="malformed XML"):
        read_junit_report(tmp_path)


def test_witness_links_are_read_one_per_line_skipping_blank_lines(tmp_path: Path) -> None:
    _write_archive_file(
        tmp_path,
        WITNESS_LINKS_FILENAME,
        '{"test": "pkg/a_test.py::test_a", "coordinate": "area.a", "partial": null}\n\n'
        '{"test": "pkg/a_test.py::test_b", "coordinate": "area.b", "partial": "only the happy path"}\n',
    )

    links = read_witness_links(tmp_path)

    assert [(link.test, link.coordinate, link.partial) for link in links] == [
        ("pkg/a_test.py::test_a", "area.a", None),
        ("pkg/a_test.py::test_b", "area.b", "only the happy path"),
    ]


def test_a_malformed_witness_link_line_is_rejected_with_its_line_number(tmp_path: Path) -> None:
    _write_archive_file(
        tmp_path, WITNESS_LINKS_FILENAME, '{"test": "ok", "coordinate": "a", "partial": null}\nnot json\n'
    )

    with pytest.raises(OutcomeInvalidError, match=":2 is not a witness link"):
        read_witness_links(tmp_path)


def test_junit_names_that_already_carry_the_node_id_are_kept_whole() -> None:
    report = parse_junit_report(
        '<testsuite><testcase classname="c" name="pkg/a_test.py::test_x[1]" file="pkg/a_test.py"/></testsuite>'
    )

    assert [case.node_id for case in report.cases] == ["pkg/a_test.py::test_x[1]"]


def test_junit_without_a_name_attribute_is_rejected() -> None:
    with pytest.raises(OutcomeInvalidError, match="lacks the name attribute"):
        parse_junit_report('<testsuite><testcase classname="c" file="pkg/a_test.py"/></testsuite>')
