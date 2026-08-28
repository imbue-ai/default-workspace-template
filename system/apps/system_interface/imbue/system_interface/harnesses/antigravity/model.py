"""antigravity's model catalog and its display-only resolver.

agy's model bar is READ-ONLY. Its `/model` is an interactive TUI picker with no scriptable
one-shot form, and `--model` applies only at launch, so there is no mid-session switch to
offer: the bar reflects what agy reports and never drives it.

ONE slot, not three. agy has no separate effort or fast axis -- the tier is baked into the
model id itself (``gemini-3.7-flash-high``), and `agy models` lists each pairing as its own
row. So every option declares ``efforts=()`` and ``supports_fast=False``, which is what makes
the bar render the model chip alone (the shown slots are decided purely by the matched
option's data).
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult

# =============================================================================================
# !!! THIS LIST MUST BE UPDATED WHEN GOOGLE SHIPS NEW ANTIGRAVITY MODELS !!!
#
# Pinning the agy BINARY does not pin this. `agy models` fetches the list over the network
# (it prints "Fetching available models..." first), so it is account- and server-side: Google
# can add or retire models with no version bump, and `update_policy = "NEVER"` does nothing
# to stop that.
#
# Captured from a signed-in `agy models` on 2026-08-21 (agy 1.1.16). Left column is agy's own
# id, right column its display name -- copied verbatim, both.
#
# A model missing from this list still renders, via the derived label in `derived_option`
# below -- but that derivation is STRING PARSING OF AN ID AND IS NOT ROBUST. It reconstructs
# "Gemini 3.8 Flash (High)" from "gemini-3.8-flash-high" by rule, so it cannot know about
# things like Claude Sonnet's "(Thinking)" suffix or GPT-OSS's capitalisation, and it will
# silently produce a slightly-wrong name for any family whose naming does not follow the
# gemini pattern. It exists so a new default model degrades to a readable name instead of a
# shrug -- NOT as a substitute for updating this list. Re-run `agy models` in a signed-in
# workspace and paste the output here whenever agy is bumped.
# =============================================================================================
_MODEL_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
    ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
    ("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
    ("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"),
    ("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
    ("gemini-3.6-flash-low", "Gemini 3.6 Flash (Low)"),
    ("gemini-3.5-flash-high", "Gemini 3.5 Flash (High)"),
    ("gemini-3.5-flash-medium", "Gemini 3.5 Flash (Medium)"),
    ("gemini-3.5-flash-low", "Gemini 3.5 Flash (Low)"),
    ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
    ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
)

# agy writes its live state at the agent state-dir root (the statusline command runs with
# MNGR_AGENT_STATE_DIR), so the shared reader/watch path needs no subdirectory.
ANTIGRAVITY_STATE_RELATIVE_PATH: Final[Path] = Path(".")

ANTIGRAVITY_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=tuple(
        # ``harness_reported_model_id`` is the LABEL, not the id: agy's statusline payload
        # reports its model as a display name ("Gemini 3.7 Flash (High)"), not the
        # `gemini-3.7-flash-high` slug it lists in `agy models` -- verified against a live
        # 1.1.19 payload. The id stays the slug because that is agy's own identifier; this
        # field is exactly the seam for a harness whose reported id differs from it (claude
        # sets it for the same reason).
        ModelOption(id=model_id, label=label, harness_reported_model_id=label, efforts=(), supports_fast=False)
        for model_id, label in _MODEL_ROWS
    ),
    switch_mode=SwitchMode.READ_ONLY,
    # LIST is the honest presentation of a small hand-written set. It is never rendered --
    # a READ_ONLY bar opens no picker -- but the field is not optional and SEARCH would be a
    # lie about the set's size.
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Antigravity",
    # agy's tap does not restart: it ends the turn with Escape and then delivers the block we
    # were holding, so the entries never blink out (contract E1). Codex's shape, not claude's.
    native_atomic_shoulder_tap_possible=True,
)

# A tier suffix agy bakes into the id, rendered parenthesised in its display name.
_TIER_SUFFIXES: Final[tuple[str, ...]] = ("high", "medium", "low")
# A dotted version agy writes hyphenated in the id ("3-7" -> "3.7"), but only between digits,
# so "gpt-oss-120b" is left alone.
_VERSION_PAIR_RE: Final = re.compile(r"(?<=\d)-(?=\d)")


def derived_option(model_id: str) -> ModelOption:
    """A best-effort option for a model id absent from :data:`_MODEL_ROWS`.

    STRING PARSING, AND DELIBERATELY NOT AUTHORITATIVE -- see the banner above. A new model
    (especially a new DEFAULT, which is when this bites) would otherwise show the unrecognized
    shrug and tell the user nothing; a slightly-wrong name is strictly better than that. It is
    marked ``in_picker=False`` because it is a rendering fallback, not an offer.
    """
    # agy reports a DISPLAY NAME, so the common case needs no reconstruction at all -- use it
    # verbatim. The slug path below stays for an id that really is a slug (a hand-set
    # settings.json, or a future agy that reports one).
    if " " in model_id:
        return ModelOption(
            id=model_id, label=model_id, harness_reported_model_id=model_id,
            efforts=(), supports_fast=False, in_picker=False,
        )
    words = _VERSION_PAIR_RE.sub(".", model_id).split("-")
    tier = words.pop().title() if len(words) > 1 and words[-1] in _TIER_SUFFIXES else None
    label = " ".join(word.title() for word in words)
    return ModelOption(
        id=model_id,
        label=f"{label} ({tier})" if tier else label,
        harness_reported_model_id=model_id,
        efforts=(),
        supports_fast=False,
        in_picker=False,
    )


class AntigravityModelResolver(HarnessModelResolver):
    """Display-only: agy's model is read from the uniform ``model_state.json`` like every
    other harness, and there is nothing to write back."""

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityModelResolver":
        return cls()

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Unreachable through the UI (a READ_ONLY bar never offers a pick) -- this answers the
        # endpoint if something calls it anyway, rather than silently pretending to switch.
        return SwitchResult(ok=False, detail="Antigravity's model is changed from the agent's terminal.")
