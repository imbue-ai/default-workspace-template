"""End-to-end tests for ``mngr behaviors matrix``.

Each test builds a synthetic corpus and a synthetic test tree under ``tmp_path``
and points the command at both; the command shells out to a real inner
``pytest --collect-only`` over the synthetic tests. Nothing here touches a real
corpus or test tree.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from imbue.mngr_behaviors.cli import behaviors
from imbue.mngr_behaviors.testing import write_behavior_corpus

_MATRIX_CORPUS = {
    "browser-authorization/signin.feature": (
        "Feature: Sign-in\n"
        "\n"
        "  @fresh-code\n"
        "  Scenario: Opening a fresh login URL signs the user in\n"
        "    Given the user is not signed in\n"
        "    Then the user is signed in\n"
        "\n"
        "  @missing-code\n"
        "  Scenario Outline: Requests without a code are malformed\n"
        '    When a request is made to "<path>"\n'
        "    Then it is rejected\n"
        "\n"
        "    Examples:\n"
        "      | path   |\n"
        "      | /login |\n"
        "\n"
        "  @installation-bound\n"
        "  Rule: Only tokens minted by this installation are accepted\n"
    ),
}


def _write_tests_dir(tests_dir: Path, test_file_body: str) -> Path:
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_witnessing.py").write_text(test_file_body, encoding="utf-8")
    return tests_dir


def test_behaviors_matrix_reports_full_partial_and_none_coverage(tmp_path: Path) -> None:
    root = write_behavior_corpus(tmp_path / "behaviors", _MATRIX_CORPUS)
    tests_dir = _write_tests_dir(
        tmp_path / "tests",
        (
            "import pytest\n"
            "\n"
            '@pytest.mark.witnesses("browser-authorization.fresh-code")\n'
            "def test_fresh_code() -> None:\n"
            "    pass\n"
            "\n"
            '@pytest.mark.witnesses("browser-authorization.missing-code", partial="only the /login path")\n'
            "def test_missing_code() -> None:\n"
            "    pass\n"
        ),
    )

    result = CliRunner().invoke(behaviors, ["matrix", "--root", str(root), "--tests", str(tests_dir)])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(record["coordinate"], record["coverage"]) for record in records] == [
        ("browser-authorization.fresh-code", "full"),
        ("browser-authorization.missing-code", "partial"),
        ("browser-authorization.installation-bound", "none"),
    ]
    fresh_code, missing_code, installation_bound = records
    assert [witness["partial"] for witness in fresh_code["witnesses"]] == [None]
    assert fresh_code["witnesses"][0]["test"].endswith("::test_fresh_code")
    assert [witness["partial"] for witness in missing_code["witnesses"]] == ["only the /login path"]
    assert installation_bound["witnesses"] == []
    # matrix records are the coverage view, not the structural one: no steps/tags/invariants.
    assert set(fresh_code.keys()) == {"coordinate", "kind", "name", "file", "line", "coverage", "witnesses"}


def test_behaviors_matrix_defaults_tests_root_to_corpus_parent(tmp_path: Path) -> None:
    # A corpus at <project>/behaviors is witnessed by <project>'s tests, so omitting
    # --tests must collect witnesses from the corpus root's parent directory.
    project_dir = tmp_path / "project"
    root = write_behavior_corpus(project_dir / "behaviors", _MATRIX_CORPUS)
    _write_tests_dir(
        project_dir,
        (
            "import pytest\n"
            "\n"
            '@pytest.mark.witnesses("browser-authorization.fresh-code")\n'
            "def test_fresh_code() -> None:\n"
            "    pass\n"
        ),
    )

    result = CliRunner().invoke(behaviors, ["matrix", "--root", str(root)])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    coverage_by_coordinate = {record["coordinate"]: record["coverage"] for record in records}
    # The witness under the corpus's parent directory was collected without an explicit --tests.
    assert coverage_by_coordinate["browser-authorization.fresh-code"] == "full"


def test_behaviors_matrix_exits_zero_when_no_test_witnesses_any_unit(tmp_path: Path) -> None:
    root = write_behavior_corpus(tmp_path / "behaviors", _MATRIX_CORPUS)
    tests_dir = _write_tests_dir(tmp_path / "tests", "def test_unrelated() -> None:\n    pass\n")

    result = CliRunner().invoke(behaviors, ["matrix", "--root", str(root), "--tests", str(tests_dir)])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert {record["coverage"] for record in records} == {"none"}
    assert all(record["witnesses"] == [] for record in records)
    assert result.stderr == ""


def test_behaviors_matrix_reports_dangling_witness_on_stderr_but_still_emits_records(tmp_path: Path) -> None:
    root = write_behavior_corpus(tmp_path / "behaviors", _MATRIX_CORPUS)
    tests_dir = _write_tests_dir(
        tmp_path / "tests",
        (
            "import pytest\n"
            "\n"
            '@pytest.mark.witnesses("browser-authorization.fresh-code")\n'
            "def test_fresh_code() -> None:\n"
            "    pass\n"
            "\n"
            '@pytest.mark.witnesses("browser-authorization.no-such-unit")\n'
            "def test_dangling() -> None:\n"
            "    pass\n"
        ),
    )

    result = CliRunner().invoke(behaviors, ["matrix", "--root", str(root), "--tests", str(tests_dir)])

    assert result.exit_code == 1
    # Every unit record is still emitted on stdout, and the valid link still counts.
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["coordinate"] for record in records] == [
        "browser-authorization.fresh-code",
        "browser-authorization.missing-code",
        "browser-authorization.installation-bound",
    ]
    assert records[0]["coverage"] == "full"
    # The dangling marker is reported on stderr, keyed by the test node id.
    assert "test_dangling" in result.stderr
    assert "browser-authorization.no-such-unit" in result.stderr
    assert "matches no behavior unit" in result.stderr


def test_behaviors_matrix_rejects_a_missing_tests_root(tmp_path: Path) -> None:
    root = write_behavior_corpus(tmp_path / "behaviors", _MATRIX_CORPUS)
    missing_tests = tmp_path / "not-there"

    result = CliRunner().invoke(behaviors, ["matrix", "--root", str(root), "--tests", str(missing_tests)])

    assert result.exit_code == 1
    assert "does not exist" in result.stderr
    assert "--tests" in result.stderr
    assert result.stdout == ""
