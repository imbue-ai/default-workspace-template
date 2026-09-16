import pytest

from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.execution import evaluate_agent_node
from imbue.mngr_mapreduce.testing import make_agent_work
from imbue.mngr_mapreduce.testing import make_job_result


def _results(successful_count: int, total_count: int) -> tuple:
    return tuple(make_job_result(f"job-{idx}", is_successful=idx < successful_count) for idx in range(total_count))


def test_evaluate_node_requires_every_job_at_the_default_completion() -> None:
    """required_completion defaults to 1.0, so one unsuccessful job fails the node."""
    work = make_agent_work("map")

    assert evaluate_agent_node(work, _results(346, 346)) is NodeStatus.SUCCEEDED
    assert evaluate_agent_node(work, _results(345, 346)) is NodeStatus.FAILED


def test_evaluate_node_rounds_a_fractional_requirement_up() -> None:
    """0.9 of 346 is 311.4, and the requirement is 312 rather than 311."""
    work = make_agent_work("map", required_completion=0.9)

    assert evaluate_agent_node(work, _results(312, 346)) is NodeStatus.SUCCEEDED
    assert evaluate_agent_node(work, _results(311, 346)) is NodeStatus.FAILED


def test_evaluate_node_succeeds_vacuously_on_a_fanout_of_zero() -> None:
    """A node that found nothing has nothing to fail at, the way all([]) is true."""
    assert evaluate_agent_node(make_agent_work("filter"), ()) is NodeStatus.SUCCEEDED


def test_evaluate_node_fails_an_empty_fanout_that_expected_work() -> None:
    """min_job_count is how a node says that finding nothing is a mistake."""
    assert evaluate_agent_node(make_agent_work("discover", min_job_count=1), ()) is NodeStatus.FAILED


def test_evaluate_node_checks_min_job_count_before_the_fraction() -> None:
    """A node can be under its floor while every job it did find succeeded."""
    work = make_agent_work("discover", min_job_count=3, required_completion=0.5)

    assert evaluate_agent_node(work, _results(2, 2)) is NodeStatus.FAILED


@pytest.mark.parametrize(
    "result,expected",
    [
        (make_job_result("a"), True),
        (make_job_result("a", is_published=False), False),
        (make_job_result("a", error_summary="timed out"), False),
        (make_job_result("a", failed_gate="tests_run"), False),
    ],
)
def test_job_result_is_successful_only_when_published_clean_and_gated(result, expected: bool) -> None:
    """Acceptance is published, no error, and every gate passed."""
    assert result.is_successful is expected
