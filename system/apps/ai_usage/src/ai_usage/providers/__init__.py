"""Every AI the tracker knows how to read, one module each.

To add an AI: write ``providers/<name>.py`` that builds a ``PROVIDER`` (see ``claude.py``),
and list it below. Usage is read only for kinds a connected chat is running, so an entry
no chat uses is never asked; a chat whose kind has no entry is shown without numbers.
"""

from ai_usage.provider import UsageProvider
from ai_usage.providers import antigravity, claude, codex, opencode_go

# Keyed by the agent type Studio reports for a chat running that AI.
PROVIDERS: dict[str, UsageProvider] = {
    provider.agent_type: provider
    for provider in (
        claude.PROVIDER,
        codex.PROVIDER,
        antigravity.PROVIDER,
        opencode_go.PROVIDER,
    )
}
