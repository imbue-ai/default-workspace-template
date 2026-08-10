"""Project-specific ratchets confining postMessage to the embed contract.

All messaging between this UI and the embedding minds chrome flows through
the vendored embed contract (imported via ``src/embed.ts``); see minds'
``docs/embed-contract.md``. These ratchets are allowlist-by-file: any NEW
file that touches ``postMessage`` or registers a ``message`` listener fails
immediately, keeping the whole boundary greppable and auditable in one
place. Lives outside ``test_ratchets.py`` because that file must define the
same test set across every project (enforced by ``test_meta_ratchets.py``).
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
    rule_name="raw postMessage / message-listener usages outside the embed boundary",
    rule_description=(
        "All chrome<->workspace messaging must flow through the embed contract via src/embed.ts "
        "(sendToEmbedder / setEmbedderMessageHandler), so the whole message surface stays in one "
        "auditable place with the contract's source checks and payload validation applied. Do not "
        "call postMessage or register 'message' listeners directly -- extend the contract instead "
        "(see minds' docs/embed-contract.md)."
    ),
)

# Files sanctioned to touch the raw primitives: only the test suites, which
# stand in windows/listeners to exercise the boundary itself.
_ALLOWED_FILES = (
    "*.test.ts",
    "embed-contract.d.ts",
)


def test_prevent_raw_post_message_outside_embed_boundary() -> None:
    pattern = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), pattern, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(chunks)
