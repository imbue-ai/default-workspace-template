import contextlib
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Final, assert_never

from use_ai_integration_skill.ai_accounts import (
    accounts_root,
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
)
from use_ai_integration_skill.codex_exec import CODEX_TASK_SANDBOX, run_codex_exec
from use_ai_integration_skill.data_types import (
    AIAccount,
    AIBilling,
    AICredential,
    AICredentialKind,
    AIProvider,
    AIResult,
    CodexExecOutcome,
    CodexSandboxMode,
    LiteLLMRequest,
)
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError
from use_ai_integration_skill.litellm_call import (
    LiteLLMCaller,
    call_litellm,
    litellm_model_name,
)

DEFAULT_ANTHROPIC_MODEL: Final = "claude-haiku-4-5"
DEFAULT_OPENAI_API_MODEL: Final = "gpt-5.6-luna"
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
    """One completion with no tools, on the named account or the workspace's default one."""
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


def _default_api_model(provider: AIProvider) -> str:
    match provider:
        case AIProvider.ANTHROPIC:
            return DEFAULT_ANTHROPIC_MODEL
        case AIProvider.OPENAI:
            return DEFAULT_OPENAI_API_MODEL
        case _ as unreachable:
            assert_never(unreachable)


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


@contextlib.contextmanager
def _claude_failures_as_ai_errors() -> Generator[None, None, None]:
    try:
        yield
    except ClaudeCLITimeoutError as e:
        raise ProviderTimeoutError(str(e)) from e
    except ClaudeCLIError as e:
        raise ProviderCallError(str(e)) from e
    except FileNotFoundError as e:
        raise ProviderCallError(f"could not start claude: {e}") from e
