from collections.abc import Iterator

import pytest
from loguru import logger

from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())

# Generated harbor datasets and local job results live under this app but embed
# a full mngr-internal clone (plus arbitrary trial artifacts); pytest must
# never collect from them.
collect_ignore = ["datasets", "jobs"]

# The ROOT pytest run loads the conftests under this app too: `testpaths = ["apps/*"]` hands pytest
# this directory as an explicit argument, and the root conftest's `collect_ignore_glob` stops it
# from collecting the files below but not from descending the directories, so every conftest it
# meets on the way is imported. The root venv has no harbor, and nearly every `imbue.minds_evals`
# module imports it, so a conftest here may import from this package only through a stdlib-only
# module such as `template_loading.py`; anything else aborts every root-level run at collection.


@pytest.fixture
def captured_log_messages() -> Iterator[list[str]]:
    """Every message logged while the test runs, in order, interpolated.

    loguru writes to its own sinks rather than through `logging`, so pytest's `caplog` never sees
    it and a test that asserts on a log line has to add a sink and remove it again. The sink is
    global, so a test that raised between the two would leak it into every test after it; that is
    what this fixture's teardown is for.

    Captured from TRACE up, whatever level the caller cares about: the assertions are searches
    over the list, so the quieter records around the one being looked for cost nothing.
    """
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(message.record["message"]), level="TRACE")
    try:
        yield messages
    finally:
        logger.remove(handler_id)
