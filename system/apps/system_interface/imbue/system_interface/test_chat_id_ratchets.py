"""Project-specific ratchets guarding the chat-id / agent-id boundary.

A chat's stable :class:`ChatId` and the id of the mngr agent currently backing
it hold the same value today but are different concepts (see ``chat_registry``
and ``models.ChatId``). These ratchets keep the boundary from eroding while
the two are still equal in value:

- the frontend addresses chats through ``/api/chats`` and touches the physical
  ``/api/agents`` family only in its designated module; and
- new backend id-keyed state and signatures must pick ``AgentId`` or
  ``ChatId`` explicitly rather than accreting more untyped ``str`` ids.

Lives outside ``test_ratchets.py`` because that file must define the same test
set across every project (enforced by ``test_meta_ratchets.py``).
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_FRONTEND_SRC = Path(__file__).parent.parent.parent / "frontend" / "src"
_BACKEND_SRC = Path(__file__).parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

_PHYSICAL_ROUTE_RULE = RatchetRuleInfo(
    rule_name="frontend /api/agents literals outside the designated physical-contract module",
    rule_description=(
        "Chat operations address the stable chat id via /api/chats/<chat_id>/...; the physical "
        "/api/agents family is an internal contract that the frontend touches only in "
        "models/AgentManager.ts (the agent list and create-chat). A chat-facing call on the "
        "physical routes would keep working today (chat id == agent id) and then silently break "
        "or bypass the chat->agent resolution once a chat's backing agent can be replaced. Use "
        "the /api/chats twin, or extend models/AgentManager.ts if the call is genuinely about a "
        "physical agent."
    ),
)

# Files sanctioned to name the physical routes: the designated physical-contract
# module and the test suites (which assert on both families).
_PHYSICAL_ROUTE_ALLOWED_FILES = (
    "*.test.ts",
    "AgentManager.ts",
)

_UNTYPED_AGENT_ID_RULE = RatchetRuleInfo(
    rule_name="untyped `agent_id: str` signatures",
    rule_description=(
        "An id parameter typed plain `str` says nothing about whether it is the stable chat id "
        "or the physical agent id -- the distinction harness switching depends on. New "
        "signatures should take `ChatId` (imbue.system_interface.models) when the value "
        "identifies the user-visible chat, or `AgentId` (imbue.mngr.primitives) when it "
        "identifies the physical agent. Do not evade by renaming the parameter; pick the type "
        "that says what the value is."
    ),
)

_UNTYPED_BY_AGENT_DICT_RULE = RatchetRuleInfo(
    rule_name="untyped `_by_agent...: dict[str` declarations",
    rule_description=(
        "Per-agent registries keyed by a plain `str` hide whether their keys are chat ids or "
        "physical agent ids. New per-agent maps should be declared `dict[AgentId, ...]` and new "
        "chat-scoped maps `_by_chat...: dict[ChatId, ...]`, so every id-keyed structure states "
        "which identity it follows across a backing-agent replacement."
    ),
)

_UNTYPED_BY_CHAT_DICT_RULE = RatchetRuleInfo(
    rule_name="`_by_chat...` dicts not keyed by ChatId",
    rule_description=(
        "A map named `_by_chat...` claims chat scope, so its keys must be `ChatId` -- anything "
        "else re-opens the ambiguity the naming was meant to close."
    ),
)

_BACKEND_TEST_FILES = (
    "*_test.py",
    "test_*.py",
    "conftest.py",
    "testing.py",
)


def test_prevent_frontend_physical_route_literals() -> None:
    pattern = RegexPattern(r"""["'`]/api/agents(/|["'`])""", multiline=False)
    chunks = check_regex_ratchet(_FRONTEND_SRC, FileExtension(".ts"), pattern, _PHYSICAL_ROUTE_ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _PHYSICAL_ROUTE_RULE.format_failure(chunks)


def test_prevent_new_untyped_agent_id_signatures() -> None:
    pattern = RegexPattern(r"agent_id: str\b", multiline=False)
    chunks = check_regex_ratchet(_BACKEND_SRC, FileExtension(".py"), pattern, _BACKEND_TEST_FILES)
    assert len(chunks) <= snapshot(105), _UNTYPED_AGENT_ID_RULE.format_failure(chunks)


def test_prevent_new_untyped_by_agent_dicts() -> None:
    pattern = RegexPattern(r"_by_agent[a-z_]*\s*:\s*dict\[str", multiline=False)
    chunks = check_regex_ratchet(_BACKEND_SRC, FileExtension(".py"), pattern, _BACKEND_TEST_FILES)
    assert len(chunks) <= snapshot(13), _UNTYPED_BY_AGENT_DICT_RULE.format_failure(chunks)


def test_prevent_untyped_by_chat_dicts() -> None:
    pattern = RegexPattern(r"_by_chat[a-z_]*\s*:\s*dict\[(?!ChatId)", multiline=False)
    chunks = check_regex_ratchet(_BACKEND_SRC, FileExtension(".py"), pattern, _BACKEND_TEST_FILES)
    assert len(chunks) <= snapshot(0), _UNTYPED_BY_CHAT_DICT_RULE.format_failure(chunks)
