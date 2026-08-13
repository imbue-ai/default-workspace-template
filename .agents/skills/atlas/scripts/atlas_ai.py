#!/usr/bin/env python3
"""Atlas's out-of-band model calls -- a thin, keyless-safe wrapper.

Reuses the `claude_p` helper from the use-ai-integration skill (rather than
duplicating it) so Atlas gets the same credential resolution and isolated-cwd
behavior. Every call returns text plus cost/usage so the checkpoint clock can
enforce a per-topic token ceiling (decision 6).

The model is a top-of-file constant with a per-call override -- cheap by default,
because Atlas's model steps (topic proposal, live-tier refresh) are short and
factual.
"""

from __future__ import annotations

import sys
from pathlib import Path

CHEAP_MODEL = "claude-haiku-4-5"

# The claude_p helper ships with the use-ai-integration skill.
_UAI_SCRIPTS = Path(__file__).resolve().parents[2] / "use-ai-integration" / "scripts"


class AIUnavailable(Exception):
    """The model helper could not be loaded or the call failed."""


def complete(prompt: str, *, system: str, model: str = CHEAP_MODEL) -> dict:
    """One non-agentic completion. Returns {text, cost_usd, output_tokens}."""
    if str(_UAI_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_UAI_SCRIPTS))
    try:
        from claude_p import claude_p_completion
    except Exception as exc:  # helper missing / import error -- caller degrades
        raise AIUnavailable(f"claude_p unavailable: {exc}") from exc
    try:
        # strip_mngr_agent_vars: the call runs from a hook that carries this
        # agent's identity; the child model call must not inherit it.
        result = claude_p_completion(
            prompt, system=system, model=model, strip_mngr_agent_vars=True
        )
    except Exception as exc:
        raise AIUnavailable(f"completion failed: {exc}") from exc
    # claude_p's usage is a Usage dataclass (not a dict), so read the attribute;
    # tolerate a dict too in case the helper's shape changes.
    usage = getattr(result, "usage", None)
    if isinstance(usage, dict):
        out_tokens = int(usage.get("output_tokens", 0) or 0)
    else:
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return {
        "text": getattr(result, "text", "") or "",
        "cost_usd": getattr(result, "cost_usd", None),
        "output_tokens": out_tokens,
    }
