"""The gateway server's access log, which never records a query string."""

from typing import Final
from urllib.parse import urlsplit

from werkzeug.serving import WSGIRequestHandler

# A query string can carry a credential: the login callback's handoff token arrives as ``?token=``.
_OMITTED_QUERY_MARKER: Final[str] = "?<query omitted>"

# Control characters escaped the way werkzeug escapes them, so a crafted request line cannot forge log lines.
_CONTROL_CHARACTER_ESCAPES: Final[dict[int, str]] = {
    **{code_point: f"\\x{code_point:02x}" for code_point in [*range(0x20), *range(0x7F, 0xA0)]},
    ord("\\"): "\\\\",
}


class QueryOmittingRequestHandler(WSGIRequestHandler):
    """Werkzeug's request handler, logging each request by its path alone."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            target = urlsplit(self.path)
        except AttributeError:
            # A request line too malformed to parse sets no path, and its raw text may hold a query string.
            self.log("info", '"<unparseable request line>" %s %s', code, size)
            return
        logged_path = target.path + (_OMITTED_QUERY_MARKER if target.query else "")
        request_line = f"{self.command} {logged_path} {self.request_version}".translate(_CONTROL_CHARACTER_ESCAPES)
        self.log("info", '"%s" %s %s', request_line, code, size)
