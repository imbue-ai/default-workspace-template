# Automatic Context Compaction for Conversational Agents (`mngr-autocompact`)

## 1. Overview & Goal

This document specifies the design and implementation of automatic context compaction in `mngr` for conversational coding agents (e.g., Claude Code).

In LLM-based coding agents using Claude Code and similar architectures, prompt cache TTL remains warm for a certain lifespan (standard: 60 minutes for Claude models). Proactive context compaction triggers compaction (e.g. `/compact`) just before cache expiration (e.g., at 57 minutes of idle time), drastically reducing token costs and turn latency while preserving summary context.

The feature is designed with a decoupled architecture:
1. **`HasCompactionMixin` Capability Interface** in core `mngr`: Exposes agent capabilities for context compaction (`request_compaction()`, `get_cache_ttl_minutes()`, `get_context_tokens()`, `get_idle_since()`).
2. **Agent Harness Implementation** (e.g., `mngr_claude`): Implements `HasCompactionMixin` on `ClaudeAgent` to handle Claude-specific compaction (`/compact`), cache TTL (60 minutes), context tokens extraction from JSONL transcripts, and idle tracking.
3. **`mngr_autocompact` Plugin**: A standalone mngr plugin containing stateless staleness checking, the `mngr autocompact check` CLI command, and on-prompt compaction hooks (`on_before_send_message`).

---

## 2. Capability Interface (`HasCompactionMixin`)

Defined in `imbue.mngr.interfaces.agent`:

```python
class HasCompactionMixin(ABC):
    """Mixin for agents that support context compaction."""

    @abstractmethod
    def request_compaction(self, instructions: str | None = None) -> None:
        """Request context compaction on the agent."""

    def get_cache_ttl_minutes(self) -> int | None:
        """Get the model cache TTL in minutes, or None if unknown."""
        return None

    def get_context_tokens(self) -> int | None:
        """Get the total context size in tokens from the agent's latest turn, or None if unknown."""
        return None

    def get_idle_since(self) -> datetime | None:
        """Get the datetime when the agent became idle in the current turn, or None if not idle or already compacted."""
        return None
```

---

## 3. Compaction Modes & Triggers

The `mngr_autocompact` plugin is completely stateless, avoiding in-process background daemon threads or globals within short-lived `mngr` processes.

### Mode 1: Proactive Compaction via Periodic Run (`proactive_timer`)
* **Mechanism**: Scheduled or triggered externally (e.g., via a recurring cron job or systemd timer running `mngr autocompact run --all`).
* **Staleness Evaluation**: An agent is eligible for compaction if:
  1. Compaction mode is enabled (`PROACTIVE_TIMER`).
  2. The agent is running and currently idle (`agent.get_idle_since()` is not `None`).
  3. Idle duration $\ge (T_{\text{cache\_ttl}} - \epsilon)$ (Default: 60 min - 3 min = 57 min).
  4. Context token threshold is met (`context_tokens >= min_context_tokens` or `min_context_tokens == 0`).
* **Execution**: If stale, triggers `agent.request_compaction()`.

### Mode 2: On-Next-Prompt (`on_next_prompt`)
* **Trigger**: Hooked via `on_before_send_message(agent, host, message)`.
* **Execution**:
  1. Checks if agent is stale: idle duration $\ge (T_{\text{cache\_ttl}} - \epsilon)$ and `context_tokens >= min_context_tokens`.
  2. If stale, triggers `agent.request_compaction()` prior to message delivery.

---

## 4. CLI Commands (`mngr autocompact`)

The plugin registers CLI commands via Pluggy entry points (`register_cli_commands`):

### `mngr autocompact check`
```bash
mngr autocompact check [TARGET] [--all] [--format human|json]
```
* **Arguments & Options**:
  * `TARGET`: Optional agent address/name to check a specific agent.
  * `--all`: Check all running agents across all online hosts. Either `TARGET` or `--all` must be specified.
  * `--format`: Output format (`human` or `json`).
* **Output**:
  * Human: reports the count and names of agents requiring compaction (or "No agents require compaction.").
  * JSON: `{ "agents": ["agent-name", ...] }`.

### `mngr autocompact run`
```bash
mngr autocompact run [TARGET] [--all] [--format human|json]
```
* **Arguments & Options**:
  * `TARGET`: Optional agent address/name to compact a specific agent if stale.
  * `--all`: Compact all running agents across all online hosts if stale. Either `TARGET` or `--all` must be specified.
  * `--format`: Output format (`human` or `json`).
* **Output**:
  * Human: reports the count and names of compacted agents (or "No agents require compaction.").
  * JSON: `{ "compacted": ["agent-name", ...] }`.

---

## 5. Configuration Schema

Configurable via `settings.toml`:

```toml
[plugins.autocompact]
# Options: "disabled" (default), "proactive_timer", "on_next_prompt"
mode = "proactive_timer"

# Optional override for model cache TTL in minutes (uses agent's reported TTL if omitted)
# cache_ttl_minutes = 60

# Offset in minutes before cache expiration to trigger compaction
epsilon_offset_minutes = 3

# Minimum prompt context size (in tokens) required to trigger compaction (0 to disable gating)
min_context_tokens = 100000
```

---

## 6. Token Gating & Safety Rules

1. **Token Gating**: If `min_context_tokens > 0` and `agent.get_context_tokens()` returns `None`, compaction is **skipped** and a warning is logged.
2. **Epoch Deduping**: Once compacted in a given idle epoch, `agent.get_idle_since()` returns `None` until a new user turn completes, preventing redundant compaction cycles.
3. **Stateless Execution**: The plugin does not manage in-memory `threading.Timer` objects or mutate state on agent inspection (`mngr list`, `mngr observe`).
