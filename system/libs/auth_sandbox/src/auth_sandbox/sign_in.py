"""The browser sign-in: what the desktop's Approve does, in the sandbox (spec 6.3)."""

from collections.abc import Mapping

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from pydantic import Field, JsonValue

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
    """Initialize (the config merge keeps the template's browser entry) and run the browser sign-in."""
    latchkey.initialize()
    is_stored, detail = latchkey.auth_browser(service_name, account)
    return SignInOutcome(is_stored=is_stored, detail=detail)


@pure
def offers_browser_sign_in(services_info: Mapping[str, JsonValue]) -> bool:
    """Whether latchkey's description of a service (``services info --offline``) offers a browser sign-in.

    A description that names no ``authOptions`` (``{}`` for a service latchkey does not know) offers one, as
    ``LatchkeyServiceInfo.supports_browser`` reads it on the desktop: only a service that lists its options and
    leaves the browser out takes typed credentials instead.
    """
    auth_options = services_info.get("authOptions")
    if not isinstance(auth_options, list) or not auth_options:
        return True
    return LATCHKEY_AUTH_OPTION_BROWSER in auth_options
