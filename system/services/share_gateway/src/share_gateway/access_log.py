"""The gateway server's access log, which never records a query string."""

from typing import Final
from urllib.parse import urlsplit

from werkzeug.serving import WSGIRequestHandler

# A query string can carry a credential: the login callback's handoff token arrives as ``?token=``.
_OMITTED_QUERY_MARKER: Final[str] = "?<query omitted>"


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
        # Escaped with werkzeug's own table, as its log_request does, so a crafted request line cannot forge log lines.
        request_line = f"{self.command} {logged_path} {self.request_version}".translate(
            self._control_char_table
        )
        self.log("info", '"%s" %s %s', request_line, code, size)
