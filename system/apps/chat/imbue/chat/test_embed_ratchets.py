"""Project-specific ratchets confining the chat page's cross-frame messaging to the shared boundaries.

The chat page is an app page: it reaches the shell only through the shared library's
``app_contract.ts`` and the minds chrome only through its ``embed.ts`` (whose messages the
shell relays). Nothing in this frontend touches ``postMessage`` or registers a ``message``
listener itself; any NEW file that does fails at once, so the whole message surface stays in
the library's allowlisted files. Lives outside ``test_ratchets.py`` because that file must
define the same test set across every project (enforced by ``test_meta_ratchets.py``).
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_FRONTEND_SRC = Path(__file__).parent.parent.parent / "frontend" / "src"

pytestmark = pytest.mark.xdist_group(name="ratchets")

_RAW_POST_MESSAGE_RULE = RatchetRuleInfo(
    rule_name="raw postMessage / message-listener usages in the chat frontend",
    rule_description=(
        "Cross-frame messaging must flow through the shared library's boundary modules: the shell "
        "through app_contract.ts (connectToShell) and the minds chrome through embed.ts "
        "(sendToEmbedder / setEmbedderMessageHandler), so the whole message surface stays in "
        "auditable, allowlisted files with each boundary's source checks and payload validation "
        "applied. Do not call postMessage or register 'message' listeners here -- extend a boundary "
        "module in system/libs/workspace_ui instead."
    ),
)

# The test suites stand in windows and listeners to exercise the page against the boundaries.
_ALLOWED_FILES = ("*.test.ts",)

_RETIRED_ADDRESS_RULE = RatchetRuleInfo(
    rule_name="retired panel refs (chat:, terminal:, service:, url:, subagent:) in the chat frontend",
    rule_description=(
        "Everything is addressed as app:<name> or app:<name>?instance=<key> (contracts.md section 1); "
        "the per-kind refs of the old workspace are gone. Do not spell one here -- build the address "
        "with the library's addressFor instead."
    ),
)

# A string literal that starts with a retired ref prefix. Anchored on the opening quote so
# ordinary keys such as ``url: string`` and prose in comments do not count.
_RETIRED_ADDRESS_PATTERN = RegexPattern(
    r"""["'`](?:chat|chat-terminal|terminal|service|url|subagent):""", multiline=False
)


def test_prevent_raw_post_message_outside_the_shared_boundaries() -> None:
    pattern = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), pattern, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(chunks)


def test_prevent_retired_address_spellings() -> None:
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), _RETIRED_ADDRESS_PATTERN, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RETIRED_ADDRESS_RULE.format_failure(chunks)
