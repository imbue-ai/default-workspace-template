"""Project-specific ratchets confining postMessage to the sanctioned boundaries.

Cross-frame messaging flows through exactly five files, each owning one
boundary: messaging with the embedding minds chrome goes through the
vendored embed contract (imported via ``src/embed.ts``; see minds'
``docs/embed-contract.md``), the shell's side of every message crossing to
or from the frames it created (the minds relay of the workspace app model's
contracts section 11 and the ``shell:`` messages of its section 10) goes
through ``src/relay.ts``, an app page's side of that contract goes through
``src/app_contract.ts`` (the module the shell serves to every app), the
location beacons the workspace's own framed apps post go through
``src/locationBeacon.ts`` (origin-validated against the workspace's own
service origins; see that module's docstring), and the focus grant the host
sends its embedded ttyd terminals goes through ``src/views/terminalFocus.ts``
(outbound-only, one fixed payload-free message, no listener; see its
docstring). These ratchets are allowlist-by-file: any NEW file that touches
``postMessage`` or registers a ``message`` listener fails immediately,
keeping the whole message surface greppable and auditable file by file.
Lives outside ``test_ratchets.py`` because that file must define the same
test set across every project (enforced by ``test_meta_ratchets.py``).

The chat document (``src/chat/``) is an app page like any other: it reaches
the shell only through ``app_contract.ts`` and the chrome only through
``embed.ts`` (whose messages the shell relays), and the shell's bundle
imports nothing from under ``src/chat/`` -- the split phase 6 of the
workspace app model made.
"""

import re
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_FRONTEND_SRC = Path(__file__).parent.parent.parent / "frontend" / "src"
_SHELL_ENTRY = _FRONTEND_SRC / "index.ts"
_CHAT_DIR = _FRONTEND_SRC / "chat"

# A relative import specifier, as vite resolves it: ``from "./x"``, ``import("./x")``, and
# ``vi.mock("./x")`` all name a module; ``import type`` names none at runtime.
_RELATIVE_IMPORT = re.compile(r"""(?<![\w.])(?:from|import|vi\.mock)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")
_TYPE_ONLY_IMPORT = re.compile(r"""^\s*import\s+type\b""")

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
    "app_contract.ts",
    "embed-contract.d.ts",
    "locationBeacon.ts",
    "relay.ts",
    "terminalFocus.ts",
)

_SHELL_IMPORTS_CHAT_RULE = RatchetRuleInfo(
    rule_name="shell bundle files importing the chat document's sources",
    rule_description=(
        "The shell's bundle (everything reachable from src/index.ts at runtime) must import nothing "
        "from under src/chat/: the chat pages are a separate document served at the chat origin, "
        "and the shell knows them only as iframes. Move what the shell needs into a shared module "
        "outside src/chat/, or reach the chat page through the app contract."
    ),
)


def _runtime_imports(source_file: Path) -> list[Path]:
    """The modules ``source_file`` imports at runtime, resolved to files."""
    resolved: list[Path] = []
    for line in source_file.read_text().splitlines():
        if _TYPE_ONLY_IMPORT.match(line):
            continue
        for specifier in _RELATIVE_IMPORT.findall(line):
            candidate = (source_file.parent / specifier).resolve()
            for path in (candidate, candidate.with_name(f"{candidate.name}.ts"), candidate / "index.ts"):
                if path.is_file():
                    resolved.append(path)
                    break
    return resolved


def _shell_bundle_files() -> set[Path]:
    """Every source file reachable from the shell's entry through runtime imports."""
    reached: set[Path] = set()
    pending = [_SHELL_ENTRY.resolve()]
    while pending:
        source_file = pending.pop()
        if source_file in reached or source_file.suffix != ".ts":
            continue
        reached.add(source_file)
        pending.extend(_runtime_imports(source_file))
    return reached


def test_the_shell_bundle_imports_nothing_from_the_chat_document() -> None:
    offenders = sorted(
        str(source_file.relative_to(_FRONTEND_SRC))
        for source_file in _shell_bundle_files()
        if _CHAT_DIR.resolve() in source_file.parents
    )
    assert offenders == [], (
        f"{_SHELL_IMPORTS_CHAT_RULE.rule_name}: {offenders}\n\n{_SHELL_IMPORTS_CHAT_RULE.rule_description}"
    )


def test_prevent_raw_post_message_outside_embed_boundary() -> None:
    pattern = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), pattern, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(chunks)
