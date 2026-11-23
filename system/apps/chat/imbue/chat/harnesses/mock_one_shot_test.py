from collections.abc import Callable
from pathlib import Path

from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotModelTier


class ScriptedOneShotCompletion(OneShotCompletion):
    """Answers each call with the next scripted answer (an exception is raised instead), recording what was asked."""

    def __init__(self, answers: list[str | OneShotCompletionError]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []
        self.model_tiers: list[OneShotModelTier] = []
        self.thinking_token_allowances: list[int | None] = []
        # Runs during each call, for what happens elsewhere while the model is answering.
        self.on_call: Callable[[], None] | None = None

    def complete(
        self,
        account_dir: Path,
        system_prompt: str,
        prompt: str,
        *,
        model_tier: OneShotModelTier,
        thinking_token_allowance: int | None,
    ) -> str:
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        self.model_tiers.append(model_tier)
        self.thinking_token_allowances.append(thinking_token_allowance)
        if self.on_call is not None:
            self.on_call()
        answer = self.answers.pop(0)
        if isinstance(answer, OneShotCompletionError):
            raise answer
        return answer
