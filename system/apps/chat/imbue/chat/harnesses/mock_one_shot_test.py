from collections.abc import Callable
from pathlib import Path

from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotUsage


class ScriptedOneShotCompletion(OneShotCompletion):
    """Answers each call with the next scripted answer (an exception is raised instead), recording the prompts."""

    def __init__(self, answers: list[str | OneShotCompletionError]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.usages: list[OneShotUsage | None] = []
        # Runs during each call, for what happens elsewhere while the model is answering.
        self.on_call: Callable[[], None] | None = None

    def complete(self, account_dir: Path, system_prompt: str, prompt: str, usage: OneShotUsage | None = None) -> str:
        self.prompts.append(prompt)
        self.usages.append(usage)
        if self.on_call is not None:
            self.on_call()
        answer = self.answers.pop(0)
        if isinstance(answer, OneShotCompletionError):
            raise answer
        return answer
