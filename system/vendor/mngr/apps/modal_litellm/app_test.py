import sys
from pathlib import Path
from types import ModuleType

from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import is_module_within_package
from imbue.modal_app_kit.testing import modal_functions_missing_logging_bootstrap
from imbue.modal_app_kit.testing import uses_dunder_name_logger


def test_is_connection_failure_output_matches_only_connection_error_codes(app_module: ModuleType) -> None:
    assert app_module._is_connection_failure_output(
        "Error: P1001: Can't reach database server at `ep-abc-pooler.neon.tech:5432`"
    )
    assert app_module._is_connection_failure_output("Error: P1002: The database server was reached but timed out.")
    assert app_module._is_connection_failure_output("Error: P1017: Server has closed the connection.")
    assert not app_module._is_connection_failure_output("Error: P3018: A migration failed to apply.")
    assert not app_module._is_connection_failure_output("The database schema is not in sync with your Prisma schema.")


def test_entrypoint_imports_only_shipped_dependencies() -> None:
    """app.py runs in a container that has only its pip-installed set plus the
    imbue.modal_app_kit source mount; any other monorepo import would pass
    locally and crash the deployed container at import time."""
    allowed_roots = {"modal", "tenacity", "yaml", "litellm", "prisma"}
    allowed_imbue_package = "imbue.modal_app_kit"
    violations: list[str] = []
    for module_name in imported_module_names(Path(__file__).parent / "app.py"):
        root = module_name.split(".")[0]
        if root in sys.stdlib_module_names or root in allowed_roots:
            continue
        if root == "imbue" and is_module_within_package(module_name, allowed_imbue_package):
            continue
        violations.append(module_name)
    assert violations == []


def test_entrypoint_logger_is_named_under_imbue() -> None:
    """In the container the entrypoint is module ``app``, so a ``__name__`` logger would drop its INFO lines."""
    assert not uses_dunder_name_logger(Path(__file__).parent / "app.py")


def test_every_modal_function_bootstraps_logging_first() -> None:
    """The JSON root handler exists only once ``configure_logging()`` runs; a function that skips it drops its INFO lines."""
    assert modal_functions_missing_logging_bootstrap(Path(__file__).parent / "app.py") == []


def test_litellm_logging_env_updates_turn_on_json_logs_at_info(app_module: ModuleType) -> None:
    assert app_module._litellm_logging_env_updates({}) == {"JSON_LOGS": "1", "LITELLM_LOG": "INFO"}


def test_litellm_logging_env_updates_preserve_operator_supplied_knobs(app_module: ModuleType) -> None:
    # A dev env debugging the proxy can raise LiteLLM's level through the
    # stamped secret; the default must not clobber it.
    assert app_module._litellm_logging_env_updates({"LITELLM_LOG": "DEBUG"}) == {"JSON_LOGS": "1"}
    assert app_module._litellm_logging_env_updates({"JSON_LOGS": "", "LITELLM_LOG": "DEBUG"}) == {}


def test_litellm_config_enables_json_logs(app_module: ModuleType) -> None:
    assert app_module.LITELLM_CONFIG["litellm_settings"]["json_logs"] is True


def test_litellm_sentry_env_updates_are_empty_without_a_dsn(app_module: ModuleType) -> None:
    # No DSN (unprovisioned tier) must leave the env untouched so LiteLLM's
    # native sentry callback re-init never activates.
    assert app_module._litellm_sentry_env_updates({}) == {}
    assert app_module._litellm_sentry_env_updates({"LITELLM_SENTRY_DSN": ""}) == {}


def test_litellm_sentry_env_updates_respect_the_kill_switch(app_module: ModuleType) -> None:
    environ = {"LITELLM_SENTRY_DSN": "https://k@bugsink.invalid/2", "MINDS_SENTRY_DISABLED": "1"}
    assert app_module._litellm_sentry_env_updates(environ) == {}


def test_litellm_sentry_env_updates_pin_dsn_trace_rate_and_environment(app_module: ModuleType) -> None:
    # LiteLLM's re-init defaults to traces_sample_rate=1.0 and
    # environment="production"; both must be pinned via its own env vars.
    environ = {"LITELLM_SENTRY_DSN": "https://k@bugsink.invalid/2", "MNGR_DEPLOY_ENV": "staging"}
    assert app_module._litellm_sentry_env_updates(environ) == {
        "SENTRY_DSN": "https://k@bugsink.invalid/2",
        "SENTRY_API_TRACE_RATE": "0.0",
        "SENTRY_ENVIRONMENT": "staging",
    }


def test_litellm_sentry_env_updates_preserve_operator_supplied_knobs(app_module: ModuleType) -> None:
    # Values already in the environment (e.g. from the stamped secret) win.
    environ = {
        "LITELLM_SENTRY_DSN": "https://k@bugsink.invalid/2",
        "SENTRY_API_TRACE_RATE": "0.5",
        "SENTRY_ENVIRONMENT": "custom-env",
    }
    assert app_module._litellm_sentry_env_updates(environ) == {"SENTRY_DSN": "https://k@bugsink.invalid/2"}
