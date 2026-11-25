from typing import Final

GOOGLE_LANE: Final = "google"


class AICallError(Exception):
    """A model call from code could not be made, or did not succeed."""


class NoUsableAccountError(AICallError):
    """No signed-in account can serve the call."""


class UnsupportedProviderError(AICallError):
    """The account's provider cannot be called from code."""

    def __init__(self, lane: str, provider_label: str) -> None:
        self.lane = lane
        self.provider_label = provider_label
        super().__init__(unsupported_provider_message(lane, provider_label))


class ProviderCallError(AICallError):
    """The provider's CLI or API ran and failed, or gave no answer."""


class ProviderTimeoutError(AICallError, TimeoutError):
    """The call did not finish within its timeout."""


def unsupported_provider_message(lane: str, provider_label: str) -> str:
    if lane == GOOGLE_LANE:
        return (
            f"{provider_label} sign-ins can never be used to call a model from code: Google's terms "
            "for its consumer sign-in do not allow wrapping its CLI in apps. Calling Gemini from code "
            "would need a Gemini API key, which is not supported yet either."
        )
    else:
        return (
            f"Calling {provider_label} from code is not supported yet; sign in to Anthropic or "
            "OpenAI to call a model from code."
        )
