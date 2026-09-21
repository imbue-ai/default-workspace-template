import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# scripts/junit_merge_passes.py imports its sibling bare (`from junit_test_summary import ...`),
# matching how it is invoked (`python3 scripts/junit_merge_passes.py`). Make that resolvable for
# pytest by adding scripts/ to sys.path before importing.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.junit_merge_passes import main  # noqa: E402
from scripts.junit_merge_passes import merge_passes  # noqa: E402
from scripts.junit_merge_passes import node_id_for  # noqa: E402
from scripts.junit_test_summary import RunStatus  # noqa: E402
from scripts.junit_test_summary import parse_junit  # noqa: E402

_PASSING = '<testcase classname="pkg.a_test" name="test_same_name" file="pkg/a_test.py" time="0.1"/>'
_FAILING = (
    '<testcase classname="pkg.a_test" name="test_same_name" file="pkg/a_test.py" time="0.1">'
    '<failure message="assert 1 == 2">traceback</failure></testcase>'
)
_OTHER_FILE_FAILING = (
    '<testcase classname="pkg.b_test" name="test_same_name" file="pkg/b_test.py" time="0.1">'
    '<failure message="always broken">traceback</failure></testcase>'
)


def _write_pass(path: Path, testcases_xml: str) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest">' + testcases_xml + "</testsuite></testsuites>"
    )
    return path


def _run_main(pass_paths: list[Path], output: Path) -> int:
    return main(["--path-prefix", "apps/x", "--output", str(output), "--pass-junit", *map(str, pass_paths)])


def test_node_id_is_the_repo_relative_file_and_the_test_name() -> None:
    testcase = ET.fromstring(_PASSING)
    assert node_id_for(testcase, "apps/x/") == "apps/x/pkg/a_test.py::test_same_name"


def test_node_id_falls_back_to_the_classname_when_pytest_wrote_no_file() -> None:
    testcase = ET.fromstring('<testcase classname="pkg.a_test" name="test_a"/>')
    assert node_id_for(testcase, "apps/x") == "apps/x/pkg/a_test.py::test_a"


def test_a_test_that_failed_in_one_pass_and_passed_in_another_reads_as_flaky_recovered(tmp_path: Path) -> None:
    first = _write_pass(tmp_path / "pass-1.xml", _FAILING)
    second = _write_pass(tmp_path / "pass-2.xml", _PASSING)
    output = tmp_path / "junit.xml"
    ET.ElementTree(merge_passes([first, second], "apps/x")).write(output)

    per_test, failures = parse_junit(output)

    record = per_test["apps/x/pkg/a_test.py::test_same_name"]
    assert (record.attempts, record.final_status) == (2, RunStatus.FLAKY_RECOVERED)
    assert [(failure.attempt, failure.message) for failure in failures] == [(1, "assert 1 == 2")]


def test_same_named_tests_in_different_files_are_not_folded_into_one(tmp_path: Path) -> None:
    """The bare function name is what pytest writes, and folding on it would let a test that passes
    in one file excuse one that always fails in another."""
    only_pass = _write_pass(tmp_path / "pass-1.xml", _PASSING + _OTHER_FILE_FAILING)
    output = tmp_path / "junit.xml"
    ET.ElementTree(merge_passes([only_pass], "apps/x")).write(output)

    per_test, _failures = parse_junit(output)

    assert {name: record.final_status for name, record in per_test.items()} == {
        "apps/x/pkg/a_test.py::test_same_name": RunStatus.PASSED,
        "apps/x/pkg/b_test.py::test_same_name": RunStatus.FAILED,
    }


def test_main_exits_zero_when_every_failure_was_recovered_in_another_pass(tmp_path: Path) -> None:
    passes = [_write_pass(tmp_path / "pass-1.xml", _FAILING), _write_pass(tmp_path / "pass-2.xml", _PASSING)]
    assert _run_main(passes, tmp_path / "out" / "junit.xml") == 0
    assert (tmp_path / "out" / "junit.xml").is_file()


def test_main_exits_one_and_names_a_test_that_never_passed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    passes = [_write_pass(tmp_path / "pass-1.xml", _FAILING), _write_pass(tmp_path / "pass-2.xml", _FAILING)]
    assert _run_main(passes, tmp_path / "junit.xml") == 1
    assert "never passed: apps/x/pkg/a_test.py::test_same_name" in capsys.readouterr().err


def test_main_exits_one_without_writing_when_a_pass_left_no_junit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session that died before writing its junit (the CI session cap, a crashed worker) must not
    be read as a pass that ran nothing."""
    passes = [_write_pass(tmp_path / "pass-1.xml", _PASSING), tmp_path / "pass-2.xml"]
    assert _run_main(passes, tmp_path / "junit.xml") == 1
    assert "pass-2.xml" in capsys.readouterr().err
    assert not (tmp_path / "junit.xml").exists()
