from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError


class ScriptedOneShotCompletion(OneShotCompletion):
    """Answers each call with the next scripted answer (an exception is raised instead), recording the prompts."""

    def __init__(self, answers: list[str | OneShotCompletionError]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def complete(self, agent_info: AgentInfo, system_prompt: str, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, OneShotCompletionError):
            raise answer
        return answer
