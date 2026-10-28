from app_manifest.suite_run import parse_failed_test_ids

_PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 7 items / 1 error
==================== short test summary info (inner run) =======================
FAILED inner/test_ignored.py::test_from_an_earlier_section - AssertionError
=========================== short test summary info ============================
FAILED pkg/test_a.py::test_one - AssertionError: assert 1 == 2
FAILED pkg/test_a.py::test_param[x-y] - ValueError: bad
FAILED pkg/test_a.py::test_param[a - b] - ValueError: [1] - worse
ERROR pkg/test_c.py::test_setup[c - d]
ERROR pkg/test_b.py - ModuleNotFoundError: No module named 'gone'
ERROR pkg/test_c.py::test_fixture
FAILED pkg/test_a.py::test_one - AssertionError: assert 1 == 2
PASSED pkg/test_a.py::test_two
=================== 4 failed, 1 passed, 3 errors in 0.52s ======================
"""


def test_failed_test_ids_come_from_the_last_short_summary_once_each_without_their_messages() -> None:
    assert parse_failed_test_ids(_PYTEST_OUTPUT) == (
        "pkg/test_a.py::test_one",
        "pkg/test_a.py::test_param[x-y]",
        "pkg/test_a.py::test_param[a - b]",
        "pkg/test_c.py::test_setup[c - d]",
        "pkg/test_b.py",
        "pkg/test_c.py::test_fixture",
    )


def test_output_without_a_short_summary_names_no_failed_tests() -> None:
    assert parse_failed_test_ids("collected 0 items\n\nno tests ran in 0.01s\n") == ()
