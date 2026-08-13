"""FastAPI app: one route that forwards the provider callback to the right env."""

import logging
import os
import re

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import RedirectResponse

from imbue.oauth_redirector.forwarding import StateParseError
from imbue.oauth_redirector.forwarding import is_allowed_forward_target
from imbue.oauth_redirector.forwarding import read_callback_url_from_state

logger = logging.getLogger(__name__)

# The tier's connector hostname pattern, baked into the deployed function
# spec at ``modal deploy`` time by the just recipe (read at module load in
# app.py, threaded through this env var to the container).
ALLOWED_HOST_REGEX_ENV = "OAUTH_REDIRECTOR_ALLOWED_HOST_REGEX"

web_app = FastAPI()


def _allowed_host_pattern() -> re.Pattern[str]:
    raw = os.environ.get(ALLOWED_HOST_REGEX_ENV, "")
    if not raw:
        raise HTTPException(status_code=503, detail=f"{ALLOWED_HOST_REGEX_ENV} is not configured")
    return re.compile(raw)


@web_app.get("/health/liveness")
def get_health_liveness() -> dict[str, str]:
    return {"status": "ok"}


@web_app.get("/forward")
def forward_provider_callback(request: Request) -> RedirectResponse:
    """Forward the OAuth provider's callback to the env named in the state's ``cb`` claim.

    The whole query string travels verbatim (code/state on success,
    error/state on cancellation); the destination connector performs the
    actual verification. Unforwardable requests get a 400 -- there is no
    fallback destination.
    """
    state = request.query_params.get("state", "")
    if not state:
        raise HTTPException(status_code=400, detail="state is required")
    try:
        callback_url = read_callback_url_from_state(state)
    except StateParseError as exc:
        raise HTTPException(status_code=400, detail=f"Unreadable state: {exc}") from exc
    if not is_allowed_forward_target(callback_url, _allowed_host_pattern()):
        logger.warning("Refused to forward an OAuth callback to %s", callback_url)
        raise HTTPException(status_code=400, detail="The state's callback URL is not an allowed forward target")
    query = request.url.query
    return RedirectResponse(url=f"{callback_url}?{query}", status_code=302)
