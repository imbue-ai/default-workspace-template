from typing import Final

SIGN_IN_ADVICE: Final = "sign in to an AI provider to call a model from code."


class AICallError(Exception):
    """A model call from code could not be made, or did not succeed."""


class NoUsableAccountError(AICallError):
    """No signed-in account can serve the call."""


class UnsupportedProviderError(AICallError):
    """The account is of a type the library does not know."""

    def __init__(self, lane: str, provider_label: str) -> None:
        self.lane = lane
        self.provider_label = provider_label
        super().__init__(
            f"Calling {provider_label} from code is not supported yet; use another account."
        )


class EmptySystemPromptError(AICallError, ValueError):
    """A completion was given no system prompt to frame it."""


class ModelRequiredError(AICallError):
    """The call names no model and its provider has no default one."""


class ProviderCallError(AICallError):
    """The provider's CLI or API ran and failed, or gave no answer."""


class ProviderTimeoutError(AICallError, TimeoutError):
    """The call did not finish within its timeout."""
