"""The two completion backends: the direct Anthropic API and headless ``claude -p``.

Both are ``async``. The direct-API path uses ``AsyncAnthropic`` and enables prompt
caching on the system prompt. The ``claude -p`` path runs the CLI as a blocking
subprocess offloaded to a worker thread via ``anyio`` (no raw asyncio), reading
its ``--output-format json`` usage/cost so callers can price and compare.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence

from anthropic import AsyncAnthropic
from anyio import to_thread

from ai_integration.data_types import BillingPath, CompletionResult, ToolCall, Usage
from ai_integration.errors import ClaudeCLIError
from ai_integration.pricing import estimate_cost_usd


async def complete_via_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1024,
    options: Mapping[str, object] | None = None,
) -> CompletionResult:
    """One non-agentic completion through the direct Anthropic API.

    ``options`` is passed straight through to ``messages.create`` so any Anthropic
    API parameter (tools, response formats, temperature, etc.) is usable. The
    system prompt is sent as a cache-controlled block to enable prompt caching.
    """
    kwargs: dict[str, object] = dict(options or {})
    kwargs.setdefault("model", model)
    kwargs.setdefault("max_tokens", max_tokens)
    kwargs.setdefault("messages", [{"role": "user", "content": prompt}])
    if system is not None and "system" not in kwargs:
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    # ``async with`` so the client's httpx connection pool is always released --
    # a new client is built per call, and leaking the pool would accumulate open
    # connections across a high-volume completion flow.
    async with AsyncAnthropic(api_key=api_key) as client:
        response = await client.messages.create(**kwargs)  # type: ignore[arg-type]
    return build_api_result(response, model)


def build_api_result(response: object, requested_model: str) -> CompletionResult:
    """Assemble a ``CompletionResult`` from an Anthropic ``messages.create`` response.

    Pure and duck-typed (reads ``.content`` / ``.usage`` / ``.model``) so it is
    unit-testable without a live ``AsyncAnthropic`` client. The reported ``model`` is
    the *served* model (``response.model``) -- which honors ``CompletionResult.model``'s
    "served by" contract, since an alias can resolve to a dated snapshot -- falling
    back to ``requested_model`` only if the response omits it. Cost is estimated from
    the served model's price so the figure matches what was actually billed.
    """
    text, tool_calls = parse_api_content(getattr(response, "content", []))
    usage_obj = getattr(response, "usage", None)
    usage = Usage(
        input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
        output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
    )
    served_model = getattr(response, "model", None)
    model = served_model if isinstance(served_model, str) and served_model else requested_model
    return CompletionResult(
        text=text,
        billing_path=BillingPath.DIRECT_API,
        model=model,
        tool_calls=tool_calls,
        usage=usage,
        cost_usd=estimate_cost_usd(model, usage),
    )


def _as_int(value: object) -> int:
    """Coerce a JSON value to an int, treating anything non-numeric as 0."""
    return int(value) if isinstance(value, (int, float)) else 0


def _str_keyed(value: object) -> dict[str, object]:
    """Materialize a ``dict[str, object]`` from an arbitrary JSON value.

    ``json.loads`` output is statically ``object``; coercing keys to ``str`` here
    gives the rest of the parser a precisely-typed mapping to read from (rather
    than indexing an ``Unknown``-keyed dict that the type checker rejects).
    """
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def parse_api_content(content: Sequence[object]) -> tuple[str, tuple[ToolCall, ...]]:
    """Split an Anthropic ``messages.create`` response into text and tool calls.

    Concatenates ``text`` blocks into the plain completion text and collects
    ``tool_use`` blocks (the structured-output channel) into ``ToolCall``s. Pure and
    duck-typed (reads ``.type`` / ``.text`` / ``.id`` / ``.name`` / ``.input``) so it
    is unit-testable without the SDK. A forced tool call yields empty text and a
    populated tuple, so the structured-output data is surfaced rather than lost.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "") or ""),
                    name=str(getattr(block, "name", "") or ""),
                    input=_str_keyed(getattr(block, "input", {})),
                )
            )
    return "".join(text_parts), tuple(tool_calls)


def parse_cli_result(data: object, model: str) -> CompletionResult:
    """Build a ``CompletionResult`` from ``claude -p --output-format json`` output."""
    if not isinstance(data, Mapping):
        raise ClaudeCLIError("claude -p JSON output was not an object")
    payload = _str_keyed(data)
    usage_dict = _str_keyed(payload.get("usage"))
    usage = Usage(
        input_tokens=_as_int(usage_dict.get("input_tokens")),
        output_tokens=_as_int(usage_dict.get("output_tokens")),
        cache_read_tokens=_as_int(usage_dict.get("cache_read_input_tokens")),
        cache_write_tokens=_as_int(usage_dict.get("cache_creation_input_tokens")),
    )
    cost = payload.get("total_cost_usd")
    text = payload.get("result")
    return CompletionResult(
        text=text if isinstance(text, str) else "",
        billing_path=BillingPath.CLAUDE_CLI,
        model=model,
        usage=usage,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def build_claude_cli_argv(
    *,
    prompt: str,
    model: str,
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
    extra_args: Sequence[str] | None,
) -> list[str]:
    """Build the ``claude -p`` argv. Pure, so flag emission is unit-testable.

    ``--system-prompt`` *replaces* the default Claude Code system prompt;
    ``--append-system-prompt`` adds to it. ``--tools ""`` disables all tools.
    ``tools`` is checked against ``None`` (not falsiness) because the empty string
    is the meaningful "disable every tool" value, distinct from "leave the flag off
    and inherit the default tool set".

    ``permission_mode`` maps to ``--permission-mode``. Headless ``claude -p`` cannot
    prompt a human, so a tool that would need approval is otherwise auto-denied --
    which is why an agentic ``run_task`` defaults this to ``bypassPermissions`` (no
    flag is emitted when it is ``None``).
    """
    argv = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if system is not None:
        argv += ["--system-prompt", system]
    if append_system is not None:
        argv += ["--append-system-prompt", append_system]
    if tools is not None:
        argv += ["--tools", tools]
    if permission_mode is not None:
        argv += ["--permission-mode", permission_mode]
    argv += list(extra_args or [])
    return argv


def _run_claude_cli_blocking(
    *,
    prompt: str,
    model: str,
    env: Mapping[str, str],
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
    cwd: str | None,
    extra_args: Sequence[str] | None,
) -> object:
    argv = build_claude_cli_argv(
        prompt=prompt,
        model=model,
        system=system,
        append_system=append_system,
        tools=tools,
        permission_mode=permission_mode,
        extra_args=extra_args,
    )
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=dict(env), check=False, cwd=cwd
    )
    if proc.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise ClaudeCLIError(f"claude -p output was not valid JSON: {exc}") from exc


async def complete_via_cli(
    *,
    model: str,
    prompt: str,
    env: Mapping[str, str],
    system: str | None = None,
    append_system: str | None = None,
    tools: str | None = None,
    permission_mode: str | None = None,
    cwd: str | None = None,
    extra_args: Sequence[str] | None = None,
) -> CompletionResult:
    """One completion/agentic run through headless ``claude -p``.

    Runs the CLI in a worker thread (so the async caller isn't blocked) and parses
    its JSON usage/cost. ``env`` should be built via
    ``credentials.build_claude_cli_env`` so ``MAIN_CLAUDE_SESSION_ID`` is unset.

    ``cwd`` sets the subprocess working directory. ``claude -p`` auto-discovers the
    project ``CLAUDE.md`` and ``.claude`` hooks from the working directory, so the
    non-agentic completion path passes an isolated temp dir to keep that ambient
    project context (and its hook-injected reminders) out of the prompt. ``None``
    inherits the caller's cwd, which is what the agentic ``run_task`` path wants.

    ``system`` maps to ``--system-prompt`` (replacing Claude Code's default agent
    system prompt) and ``append_system`` to ``--append-system-prompt``. ``tools``
    maps to ``--tools`` -- pass ``""`` to disable all tools (the non-agentic
    completion path does this so the call answers the prompt and nothing else).
    These flags are how a non-agentic ``claude -p`` call sheds most of the default
    agent's per-call context overhead; note they do *not* drop the auto-discovered
    CLAUDE.md / skills, which only ``--bare`` removes -- and ``--bare`` requires an
    API key, so it is unavailable on the keyless subscription path.
    """
    data = await to_thread.run_sync(
        lambda: _run_claude_cli_blocking(
            prompt=prompt,
            model=model,
            env=env,
            system=system,
            append_system=append_system,
            tools=tools,
            permission_mode=permission_mode,
            cwd=cwd,
            extra_args=extra_args,
        )
    )
    return parse_cli_result(data, model)
