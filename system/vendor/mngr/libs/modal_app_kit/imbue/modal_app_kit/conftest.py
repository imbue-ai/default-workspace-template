import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sentry_sdk

from imbue.modal_app_kit.log_format import IMBUE_LOGGER_NAME
from imbue.modal_app_kit.log_format import _configure_logging_once


@pytest.fixture(scope="session")
def sentry_sdk_integrations_imported() -> None:
    """Pay the SDK's once-per-process first-``init`` cost outside any test's timeout.

    That first init imports every auto-enabling integration (openai, fastapi,
    ...; seconds of pydantic model construction, longer on a cold CI disk
    cache), which would otherwise land inside whichever SDK-activating test
    runs first in a worker and race the package's per-test timeout.
    ``timeout_func_only`` keeps fixture setup off that clock. The empty DSN
    skips the ``SENTRY_DSN`` environment fallback so no transport is created.
    """
    sentry_sdk.init(dsn="")
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def isolated_sentry_client(sentry_sdk_integrations_imported: None) -> Iterator[None]:
    """Clear the sentry-sdk global client around a test that installs or inspects one.

    The clear runs both before (isolating from any client a previous test
    left behind) and after (even when the test fails mid-assertion, which an
    inline trailing reset would miss).
    """
    sentry_sdk.get_global_scope().set_client(None)
    yield
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def no_minds_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_ENV_NAME", raising=False)


@pytest.fixture
def throwaway_logger() -> Iterator[logging.Logger]:
    """A fresh, uniquely named, non-propagating logger standing in for root or a module logger.

    Keeps the real root logger (and pytest's capture) untouched; whatever
    handlers the test attaches are stripped again on teardown.
    """
    throwaway = logging.getLogger(f"throwaway-{uuid4().hex}")
    throwaway.propagate = False
    yield throwaway
    for handler in list(throwaway.handlers):
        throwaway.removeHandler(handler)


@pytest.fixture
def restored_root_logger() -> Iterator[None]:
    """Restore the real root / ``imbue`` logger state around a test that runs ``configure_logging``.

    The once-per-process cache is cleared on both sides so the test sees a
    fresh install and leaves none behind for later tests in the worker.
    """
    root = logging.getLogger()
    imbue_logger = logging.getLogger(IMBUE_LOGGER_NAME)
    original_handlers = list(root.handlers)
    original_root_level = root.level
    original_imbue_level = imbue_logger.level
    _configure_logging_once.cache_clear()
    yield
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
    root.setLevel(original_root_level)
    imbue_logger.setLevel(original_imbue_level)
    _configure_logging_once.cache_clear()
