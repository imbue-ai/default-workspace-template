"""The gateway server's request handler: no access log of its own.

Every request caddy forwards to the gateway is already recorded by caddy's access log (``caddyfile.py``), with the
service's host, the status and the response size the gateway's own handler never sees, so a second line per
``/_auth/verify`` subrequest here would only double the volume. Denials are logged by the verify handler itself,
and werkzeug's error lines still reach the service's stderr.
"""

from werkzeug.serving import WSGIRequestHandler


class SilentRequestHandler(WSGIRequestHandler):
    """Werkzeug's request handler with the per-request access line dropped."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        return None
