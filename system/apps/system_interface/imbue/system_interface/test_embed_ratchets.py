"""Project-specific ratchets confining postMessage to the sanctioned boundaries.

Cross-frame messaging flows through exactly three files, each owning one
boundary: messaging with the embedding minds chrome goes through the
vendored embed contract (imported via ``src/embed.ts``; see minds'
``docs/embed-contract.md``), the location beacons the workspace's own
framed apps post go through ``src/locationBeacon.ts`` (origin-validated
against the workspace's own service origins; see that module's docstring),
and the focus grant the host sends its embedded ttyd terminals goes through
``src/views/terminalFocus.ts`` (outbound-only, one fixed payload-free
message, no listener; see its docstring). These ratchets are
allowlist-by-file: any NEW file that touches ``postMessage`` or registers a
``message`` listener fails immediately, keeping the whole message surface
greppable and auditable file by file. Lives outside ``test_ratchets.py``
because that file must define the same test set across every project
(enforced by ``test_meta_ratchets.py``).
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
    rule_name="raw postMessage / message-listener usages outside the sanctioned boundaries",
    rule_description=(
        "Cross-frame messaging must flow through a sanctioned boundary module: chrome<->workspace "
        "through the embed contract via src/embed.ts (sendToEmbedder / setEmbedderMessageHandler), "
        "and the framed apps' location beacons through src/locationBeacon.ts -- so the whole "
        "message surface stays in auditable, allowlisted files with each boundary's source checks "
        "and payload validation applied. Do not call postMessage or register 'message' listeners "
        "anywhere else -- extend a boundary module instead (see minds' docs/embed-contract.md and "
        "locationBeacon.ts's own docstring)."
    ),
)

# Files sanctioned to touch the raw primitives: the boundary modules (each
# owning one documented channel) and the test suites, which stand in
# windows/listeners to exercise the boundaries themselves.
_ALLOWED_FILES = (
    "*.test.ts",
    "embed-contract.d.ts",
    "locationBeacon.ts",
    "terminalFocus.ts",
)


def test_prevent_raw_post_message_outside_embed_boundary() -> None:
    pattern = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), pattern, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(chunks)
