import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sentry_sdk
from sentry_sdk.integrations import iter_default_integrations

from imbue.modal_app_kit.log_format import IMBUE_LOGGER_NAME
from imbue.modal_app_kit.log_format import _configure_logging_once


@pytest.fixture(scope="session", autouse=True)
def _preimported_sentry_default_integrations() -> None:
    """Import sentry-sdk's default integrations once, before any test is timed.

    ``sentry_sdk.init`` imports every default integration module on its first
    call in a process -- around twenty of them, which costs a couple of seconds
    even on a warm machine and considerably more on a cold, contended sandbox.
    Whichever test happened to init first paid that cost inside its own 10s
    budget and timed out intermittently.

    ``timeout_func_only`` is set for this package, so a session fixture's time
    is not charged to any test. ``setup_integrations`` walks this same
    iterator, so exhausting it here moves the import cost out of every
    measured window; the modules (and the third-party libraries they import at
    module scope -- openai, fastapi, and seconds of pydantic model
    construction) are then in ``sys.modules``, so the init inside a test only
    wires up already-imported classes. ``auto_enabling_integrations`` defaults
    to true, so both halves of the set are warmed.
    """
    for _integration in iter_default_integrations(True):
        pass


@pytest.fixture
def isolated_sentry_client() -> Iterator[None]:
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
