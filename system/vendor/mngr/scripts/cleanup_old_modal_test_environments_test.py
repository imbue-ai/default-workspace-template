from imbue.mngr_modal.cleanup import ModalTestEnvironmentSweepResult
from scripts.cleanup_old_modal_test_environments import describe_planned_sweep
from scripts.cleanup_old_modal_test_environments import report_sweep

_ENV_A = "mngr_test-2026-07-30-18-23-24-5b1f9a04e7c34d17"
_ENV_B = "mngr_test-2026-07-30-19-25-15-c82d6e40fa1b4937"
_ENV_C = "mngr_test-2026-08-17-22-47-45-9e3a7c15bd684f20"


def test_report_sweep_exits_non_zero_and_names_the_environments_left_behind() -> None:
    result = ModalTestEnvironmentSweepResult(
        deleted_environment_names=(_ENV_A,),
        failed_environment_names=(_ENV_B, _ENV_C),
    )

    exit_code, summary = report_sweep(result)

    assert exit_code == 1
    assert _ENV_B in summary
    assert _ENV_C in summary


def test_report_sweep_exits_zero_when_every_environment_is_gone() -> None:
    result = ModalTestEnvironmentSweepResult(
        deleted_environment_names=(_ENV_A, _ENV_B),
        failed_environment_names=(),
    )

    exit_code, summary = report_sweep(result)

    assert exit_code == 0
    assert "2" in summary


def test_report_sweep_exits_zero_when_there_was_nothing_to_do() -> None:
    exit_code, summary = report_sweep(
        ModalTestEnvironmentSweepResult(deleted_environment_names=(), failed_environment_names=())
    )

    assert exit_code == 0
    assert "No old Modal test environments" in summary


def test_describe_planned_sweep_names_every_environment_it_would_touch() -> None:
    summary = describe_planned_sweep((_ENV_A, _ENV_B))

    assert _ENV_A in summary
    assert _ENV_B in summary
    assert "2" in summary


def test_describe_planned_sweep_says_so_when_there_is_nothing_to_do() -> None:
    assert describe_planned_sweep(()) == "No old Modal test environments found to clean up"
