"""Test utilities shared across the gateway's test modules."""

from werkzeug.wrappers import Response


def set_cookies_by_name(response: Response) -> dict[str, str]:
    """The response's rendered ``Set-Cookie`` headers, keyed by cookie name."""
    return {header.split("=", 1)[0]: header for header in response.headers.getlist("Set-Cookie")}
