import contextlib
import tempfile
from collections.abc import Generator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, assert_never

from use_ai_integration_skill.agy_exec import agy_complete, agy_run_task
from use_ai_integration_skill.ai_accounts import (
    accounts_root,
    list_ai_accounts,
    read_ai_credential,
    resolve_ai_account,
)
from use_ai_integration_skill.claude_p import (
    ANTHROPIC_ENV_SNAPSHOT_PATH,
    ClaudeCLIError,
    ClaudeCLITimeoutError,
    ClaudeResult,
    WorkspaceAICredentials,
    claude_p_completion,
    claude_p_task,
    write_anthropic_env_snapshot,
)
from use_ai_integration_skill.codex_exec import CODEX_TASK_SANDBOX, run_codex_exec
from use_ai_integration_skill.data_types import (
    AgyRunOutcome,
    AIAccount,
    AIAccountListing,
    AIBilling,
    AICredential,
    AICredentialKind,
    AIProvider,
    AIResult,
    CodexExecOutcome,
    CodexSandboxMode,
    LiteLLMRequest,
    PiRunOutcome,
)
from use_ai_integration_skill.errors import (
    ModelRequiredError,
    NoUsableAccountError,
    ProviderCallError,
    ProviderTimeoutError,
)
from use_ai_integration_skill.litellm_call import (
    LiteLLMCaller,
    call_litellm,
    litellm_model_name,
)
from use_ai_integration_skill.pi_exec import pi_complete, pi_run_task

DEFAULT_ANTHROPIC_MODEL: Final = "claude-haiku-4-5"
DEFAULT_OPENAI_API_MODEL: Final = "gpt-5.6-luna"
# pi 0.87.1's default model for each provider a pi account can hold, with this library's small Anthropic and OpenAI defaults
DEFAULT_PI_MODELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "anthropic": DEFAULT_ANTHROPIC_MODEL,
        "openai": DEFAULT_OPENAI_API_MODEL,
        "ant-ling": "Ring-2.6-1T",
        "cerebras": "gpt-oss-120b",
        "deepseek": "deepseek-v4-pro",
        "fireworks": "accounts/fireworks/models/kimi-k2p6",
        "google": "gemini-3.1-pro-preview",
        "groq": "openai/gpt-oss-120b",
        "kimi-coding": "kimi-for-coding",
        "minimax": "MiniMax-M2.7",
        "minimax-cn": "MiniMax-M2.7",
        "mistral": "devstral-medium-latest",
        "moonshotai": "kimi-k2.6",
        "moonshotai-cn": "kimi-k2.6",
        "nvidia": "nvidia/nemotron-3-super-120b-a12b",
        "opencode": "kimi-k2.6",
        "opencode-go": "kimi-k2.6",
        "openrouter": "moonshotai/kimi-k2.6",
        "qwen-token-plan": "qwen3.7-max",
        "qwen-token-plan-cn": "qwen3.7-max",
        "together": "moonshotai/Kimi-K2.6",
        "vercel-ai-gateway": "zai/glm-5.1",
        "xai": "grok-4.7",
        "xiaomi": "mimo-v2.5-pro",
        "xiaomi-token-plan-ams": "mimo-v2.5-pro",
        "xiaomi-token-plan-cn": "mimo-v2.5-pro",
        "xiaomi-token-plan-sgp": "mimo-v2.5-pro",
        "zai": "glm-5.3",
        "zai-coding-cn": "glm-5.3",
    }
)
DEFAULT_COMPLETION_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_TASK_TIMEOUT_SECONDS: Final = 1800.0


def complete(
    prompt: str,
    *,
    system: str,
    model: str | None = None,
    account: str | None = None,
    timeout_seconds: float = DEFAULT_COMPLETION_TIMEOUT_SECONDS,
) -> AIResult:
    """One completion, on the named account or the workspace's default one; tool-less except on a ChatGPT or Google sign-in."""
    resolved = resolve_ai_account(account, accounts_root())
    credential = read_ai_credential(
        resolved, anthropic_snapshot_path=ANTHROPIC_ENV_SNAPSHOT_PATH
    )
    return complete_on_account(
        prompt,
        system=system,
        model=model,
        account=resolved,
        credential=credential,
        timeout_seconds=timeout_seconds,
        litellm_caller=call_litellm,
    )


def run_task(
    prompt: str,
    *,
    instructions: str | None = None,
    model: str | None = None,
    account: str | None = None,
    cwd: Path | str | None = None,
    timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
) -> AIResult:
    """One agentic task with tools and file access in ``cwd``, on the named account or the default one."""
    resolved = resolve_ai_account(account, accounts_root())
    credential = read_ai_credential(
        resolved, anthropic_snapshot_path=ANTHROPIC_ENV_SNAPSHOT_PATH
    )
    return run_task_on_account(
        prompt,
        instructions=instructions,
        model=model,
        account=resolved,
        credential=credential,
        working_dir=Path(cwd) if cwd is not None else Path.cwd(),
        timeout_seconds=timeout_seconds,
    )


def list_accounts() -> list[AIAccountListing]:
    """The workspace's accounts, with the default marked and how a call on each is billed."""
    return list_ai_accounts(
        accounts_root(), anthropic_snapshot_path=ANTHROPIC_ENV_SNAPSHOT_PATH
    )


def pin_anthropic_key(account: str | None = None) -> str:
    """Snapshot the named or default Anthropic account's API key for every Anthropic call from code; returns the snapshot's path."""
    return pin_anthropic_key_of_account(
        resolve_ai_account(account, accounts_root()),
        anthropic_snapshot_path=ANTHROPIC_ENV_SNAPSHOT_PATH,
    )


def pin_anthropic_key_of_account(
    account: AIAccount, *, anthropic_snapshot_path: str
) -> str:
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )
    if credential.kind is not AICredentialKind.ANTHROPIC_API_KEY:
        raise NoUsableAccountError(
            f"The account {account.label} is not an Anthropic account with an API key; there is no key to pin."
        )
    return write_anthropic_env_snapshot(
        anthropic_snapshot_path, account_dir=str(account.account_dir)
    )


def _default_api_model(provider: AIProvider) -> str:
    match provider:
        case AIProvider.ANTHROPIC:
            return DEFAULT_ANTHROPIC_MODEL
        case AIProvider.OPENAI:
            return DEFAULT_OPENAI_API_MODEL
        case (
            AIProvider.GOOGLE
            | AIProvider.OPENCODE_GO
            | AIProvider.OPENROUTER
            | AIProvider.API_KEY
        ):
            raise ProviderCallError(f"{provider} has no litellm default model")
        case _ as unreachable:
            assert_never(unreachable)


def _default_pi_model(provider_id: str) -> str:
    model = DEFAULT_PI_MODELS.get(provider_id)
    if model is None:
        raise ModelRequiredError(
            f"pi has no default model for the provider {provider_id!r}; pass model= to call it."
        )
    return model


def _pi_provider_id(account: AIAccount, credential: AICredential) -> str:
    if credential.pi_provider_id is None:
        raise ProviderCallError(
            f"the {credential.kind} credential of {account.label} names no pi provider"
        )
    return credential.pi_provider_id


def complete_on_account(
    prompt: str,
    *,
    system: str,
    model: str | None,
    account: AIAccount,
    credential: AICredential,
    timeout_seconds: float,
    litellm_caller: LiteLLMCaller,
) -> AIResult:
    match credential.kind:
        case AICredentialKind.ANTHROPIC_API_KEY | AICredentialKind.OPENAI_API_KEY:
            if credential.api_key is None:
                raise ProviderCallError(
                    f"the {credential.kind} credential of {account.label} holds no key"
                )
            chosen_model = model or _default_api_model(account.provider)
            reply = litellm_caller(
                LiteLLMRequest(
                    model=litellm_model_name(account.provider, chosen_model),
                    system=system,
                    prompt=prompt,
                    api_key=credential.api_key,
                    api_base=credential.api_base,
                    timeout_seconds=timeout_seconds,
                )
            )
            return AIResult(
                text=reply.text,
                provider=account.provider,
                account_id=account.account_id,
                model=chosen_model,
                usage=reply.usage,
                billing=credential.billing,
                cost_usd=reply.cost_usd,
                raw=reply.raw,
            )
        case AICredentialKind.ANTHROPIC_SIGN_IN:
            chosen_model = model or DEFAULT_ANTHROPIC_MODEL
            with _claude_failures_as_ai_errors():
                result = claude_p_completion(
                    prompt,
                    system=system,
                    model=chosen_model,
                    strip_mngr_agent_vars=True,
                    account_dir=str(account.account_dir),
                    timeout_seconds=timeout_seconds,
                    is_prompt_text_outside_argv=True,
                )
            return ai_result_from_claude(
                result, account=account, model=chosen_model, billing=credential.billing
            )
        case AICredentialKind.OPENAI_SIGN_IN:
            with tempfile.TemporaryDirectory(
                prefix="codex_exec_completion_"
            ) as isolated_dir:
                outcome = run_codex_exec(
                    f"{system}\n\n{prompt}",
                    account_dir=account.account_dir,
                    model=model,
                    sandbox_mode=CodexSandboxMode.READ_ONLY,
                    working_dir=Path(isolated_dir),
                    timeout_seconds=timeout_seconds,
                )
            return ai_result_from_codex(
                outcome, account=account, model=model, billing=credential.billing
            )
        case AICredentialKind.GOOGLE_SIGN_IN:
            agy_outcome = agy_complete(
                prompt,
                system=system,
                model=model,
                account_dir=account.account_dir,
                timeout_seconds=timeout_seconds,
            )
            return ai_result_from_agy(
                agy_outcome, account=account, model=model, billing=credential.billing
            )
        case AICredentialKind.PI_KEY:
            provider_id = _pi_provider_id(account, credential)
            chosen_model = model or _default_pi_model(provider_id)
            pi_outcome = pi_complete(
                prompt,
                system=system,
                provider_id=provider_id,
                model=chosen_model,
                account_dir=account.account_dir,
                timeout_seconds=timeout_seconds,
            )
            return ai_result_from_pi(
                pi_outcome,
                account=account,
                model=chosen_model,
                billing=credential.billing,
            )
        case _ as unreachable:
            assert_never(unreachable)


def run_task_on_account(
    prompt: str,
    *,
    instructions: str | None,
    model: str | None,
    account: AIAccount,
    credential: AICredential,
    working_dir: Path,
    timeout_seconds: float,
) -> AIResult:
    match credential.kind:
        case AICredentialKind.ANTHROPIC_API_KEY | AICredentialKind.ANTHROPIC_SIGN_IN:
            chosen_model = model or DEFAULT_ANTHROPIC_MODEL
            api_credentials = (
                WorkspaceAICredentials(
                    api_key=credential.api_key.get_secret_value(),
                    base_url=credential.api_base,
                    oauth_token=None,
                )
                if credential.api_key is not None
                else None
            )
            with _claude_failures_as_ai_errors():
                result = claude_p_task(
                    prompt,
                    append_system=instructions,
                    model=chosen_model,
                    strip_mngr_agent_vars=True,
                    account_dir=str(account.account_dir),
                    api_credentials=api_credentials,
                    cwd=str(working_dir),
                    timeout_seconds=timeout_seconds,
                    is_prompt_text_outside_argv=True,
                )
            return ai_result_from_claude(
                result, account=account, model=chosen_model, billing=credential.billing
            )
        case AICredentialKind.OPENAI_API_KEY | AICredentialKind.OPENAI_SIGN_IN:
            outcome = run_codex_exec(
                f"{instructions}\n\n{prompt}" if instructions else prompt,
                account_dir=account.account_dir,
                model=model,
                sandbox_mode=CODEX_TASK_SANDBOX,
                working_dir=working_dir,
                timeout_seconds=timeout_seconds,
            )
            return ai_result_from_codex(
                outcome, account=account, model=model, billing=credential.billing
            )
        case AICredentialKind.GOOGLE_SIGN_IN:
            agy_outcome = agy_run_task(
                prompt,
                instructions=instructions,
                model=model,
                account_dir=account.account_dir,
                working_dir=working_dir,
                timeout_seconds=timeout_seconds,
            )
            return ai_result_from_agy(
                agy_outcome, account=account, model=model, billing=credential.billing
            )
        case AICredentialKind.PI_KEY:
            provider_id = _pi_provider_id(account, credential)
            chosen_model = model or _default_pi_model(provider_id)
            pi_outcome = pi_run_task(
                prompt,
                instructions=instructions,
                provider_id=provider_id,
                model=chosen_model,
                account_dir=account.account_dir,
                working_dir=working_dir,
                timeout_seconds=timeout_seconds,
            )
            return ai_result_from_pi(
                pi_outcome,
                account=account,
                model=chosen_model,
                billing=credential.billing,
            )
        case _ as unreachable:
            assert_never(unreachable)


def ai_result_from_claude(
    result: ClaudeResult, *, account: AIAccount, model: str, billing: AIBilling
) -> AIResult:
    """The result of a ``claude -p`` run; its cost counts only when the call is billed per token."""
    return AIResult(
        text=result.text,
        provider=account.provider,
        account_id=account.account_id,
        model=model,
        usage=result.usage,
        billing=billing,
        cost_usd=result.cost_usd if billing is AIBilling.API_KEY else None,
        raw=result.raw,
    )


def ai_result_from_codex(
    outcome: CodexExecOutcome,
    *,
    account: AIAccount,
    model: str | None,
    billing: AIBilling,
) -> AIResult:
    """The result of a ``codex exec`` run, which reports no cost."""
    return AIResult(
        text=outcome.text,
        provider=account.provider,
        account_id=account.account_id,
        model=model,
        usage=outcome.usage,
        billing=billing,
        cost_usd=None,
        raw={"events": outcome.events},
    )


def ai_result_from_pi(
    outcome: PiRunOutcome, *, account: AIAccount, model: str, billing: AIBilling
) -> AIResult:
    """The result of a ``pi`` run; its cost counts only when the call is billed per token."""
    return AIResult(
        text=outcome.text,
        provider=account.provider,
        account_id=account.account_id,
        model=model,
        usage=outcome.usage,
        billing=billing,
        cost_usd=outcome.cost_usd if billing is AIBilling.API_KEY else None,
        raw={"events": outcome.events},
    )


def ai_result_from_agy(
    outcome: AgyRunOutcome,
    *,
    account: AIAccount,
    model: str | None,
    billing: AIBilling,
) -> AIResult:
    """The result of an ``agy`` run, which reports no cost."""
    return AIResult(
        text=outcome.text,
        provider=account.provider,
        account_id=account.account_id,
        model=model,
        usage=outcome.usage,
        billing=billing,
        cost_usd=None,
        raw=outcome.envelope,
    )


@contextlib.contextmanager
def _claude_failures_as_ai_errors() -> Generator[None, None, None]:
    try:
        yield
    except ClaudeCLITimeoutError as e:
        raise ProviderTimeoutError(str(e)) from e
    except ClaudeCLIError as e:
        raise ProviderCallError(str(e)) from e
    except OSError as e:
        raise ProviderCallError(f"could not start claude: {e}") from e
