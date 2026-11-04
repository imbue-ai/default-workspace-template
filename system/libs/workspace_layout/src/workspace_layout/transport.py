"""One request to a shell route over loopback, with the standard library alone.

The ``workspace-layout`` command imports this rather than the typed client, so that an agent's layout call does not pay
for importing pydantic and building the library's models.
"""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.client import HTTPMessage
from typing import IO
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure

from workspace_layout.errors import ShellUnreachableError

# How much of an answer an error quotes.
ANSWER_QUOTE_LIMIT: Final[int] = 200

HTTP_SUCCESS_RANGE: Final[range] = range(200, 300)


@pure
def quote_answer(body: Any) -> str:
    """An answer as an error message quotes it: its ``detail`` when it has one, else the whole of it, shortened."""
    quoted = body.get("detail", body) if isinstance(body, dict) else body
    return str(quoted).strip()[:ANSWER_QUOTE_LIMIT]


@pure
def refusal_detail(body: dict[str, Any] | str) -> str:
    """What a refusal says: the shell's ``detail`` whole (a 412 lists the connected clients at its end), else the
    body shortened, as a proxy's error page is."""
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"]).strip()
    return quote_answer(body)


@pure
def _json_object_or_text(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed if isinstance(parsed, dict) else text


class _RedirectsAsAnswersHandler(urllib.request.HTTPRedirectHandler):
    """Declines every redirect, so a 3xx reaches the caller as an HTTPError carrying the shell's own answer."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


_SHELL_OPENER: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(_RedirectsAsAnswersHandler)


def _exchange(request: urllib.request.Request, timeout_seconds: float) -> tuple[int, bytes]:
    try:
        with _SHELL_OPENER.open(request, timeout=timeout_seconds) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def exchange_with_shell(
    method: str, url: str, body: Mapping[str, Any] | None, timeout_seconds: float
) -> tuple[int, dict[str, Any] | str]:
    """One request to a shell route, answered as it came: the status, and the body as a JSON object or else its text.
    Raises ShellUnreachableError when it could not be made (refused, timed out, or cut off)."""
    data = None if body is None else json.dumps(dict(body)).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
    except ValueError as e:
        raise ShellUnreachableError(str(e)) from e
    try:
        status_code, raw = _exchange(request, timeout_seconds)
    except (OSError, http.client.HTTPException) as e:
        raise ShellUnreachableError(str(e) or type(e).__name__) from e
    return status_code, _json_object_or_text(raw.decode("utf-8", errors="replace"))
