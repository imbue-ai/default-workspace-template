from __future__ import annotations

from tk_command_parsing.uv_run import unwrap_uv_run


def test_passthrough_options_are_skipped_to_reach_the_program() -> None:
    assert unwrap_uv_run(["uv", "run", "--no-sync", "--quiet", "tk", "start", "x"]) == (
        "tk",
        "start",
        "x",
    )


def test_an_option_that_changes_the_run_stays_in_front_of_the_program() -> None:
    assert unwrap_uv_run(["uv", "run", "--no-sync", "--with", "httpx", "tk"]) == (
        "--with",
        "httpx",
        "tk",
    )


def test_a_command_that_is_not_uv_run_is_not_unwrapped() -> None:
    assert unwrap_uv_run(["uvx", "run", "tk"]) is None
    assert unwrap_uv_run(["uv", "sync"]) is None
