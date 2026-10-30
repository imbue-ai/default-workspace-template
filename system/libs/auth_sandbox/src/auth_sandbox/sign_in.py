"""The browser sign-in: what the desktop's Approve does, in the sandbox (spec 6.3)."""

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from auth_sandbox.interfaces import SignInLatchkeyInterface


class SignInOutcome(FrozenModel):
    """How a sign-in or a credential entry ended."""

    is_stored: bool = Field(description="Whether latchkey stored a credential")
    detail: str = Field(
        description="Latchkey's reason when it did not, condensed for the dialog; empty on success"
    )


def run_sign_in(
    latchkey: SignInLatchkeyInterface, service_name: str, account: str | None
) -> SignInOutcome:
    """Initialize (the config merge keeps the template's browser entry) and run the browser sign-in.

    ``initialize`` runs on every sign-in rather than once at boot so a sign-in never trusts a config written by a
    different build of this library than the one running it.
    """
    latchkey.initialize()
    is_stored, detail = latchkey.auth_browser(service_name, account)
    return SignInOutcome(is_stored=is_stored, detail=detail)
