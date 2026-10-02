import json

from imbue.imbue_common.pure import pure


@pure
def refusal_reason(response_text: str) -> str:
    """The ``detail`` of another app's error body (the chat app's, the shell's), else the body itself."""
    try:
        body = json.loads(response_text)
    except ValueError:
        return response_text
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail is not None else response_text
