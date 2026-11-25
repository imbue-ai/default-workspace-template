"""What the hosted web client's own page may call in a shared workspace, from its own origin.

The web client is a page of another origin than the workspace's (``SHARE_CHROME_ORIGIN``). It calls two things
itself, with the workspace's session cookie: the workspace routes an internal app serves (``/api/workspace/...``),
and the gateway's own routes for visiting agents (``/_agent/...``). For exactly those requests the gateway does
three things a browser needs before a page of another origin may send a request and read its answer:

- caddy answers the browser's permission check (``OPTIONS``) itself, so it never reaches the app, whose request
  guard refuses every request of another origin;
- caddy adds the web client's origin and the credentials allowance to every answer, a refusal included, so the page
  can tell a lapsed session (401) from a workspace it cannot reach;
- ``/_auth/verify`` marks the identity it stamps on a workspace route's request, which is the word the app's request
  guard admits the request on (``workspace_layout.identity``).

A program that is not a browser can send any ``Origin`` and gains nothing: it holds no session, and the mark is set
only on a request whose session the gateway accepted. A client cannot set the mark either, since caddy strips the
identity header from every request that arrives.
"""

# The workspace routes, on whichever internal app serves them. ``system/test_web_client_routes.py`` holds this and
# the constants below to what the shell serves and its request guard reads.
WEB_CLIENT_ROUTE_PATH_PREFIX = "/api/workspace/"

# The gateway's own routes for visiting agents, on the auth label.
AGENT_ROUTE_PATH_PREFIX = "/_agent/"

# The field of ``X-Imbue-Identity`` that says the web client's page sent the request.
WEB_CLIENT_MARK_FIELD = "is_from_web_client"

# What caddy answers the browser's permission check with: the methods the routes use, and the one header a JSON
# request adds that a browser asks permission for.
WEB_CLIENT_ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE"
WEB_CLIENT_ALLOWED_HEADERS = "Content-Type"
# How long a browser may keep the answer, so each call does not cost a second request.
WEB_CLIENT_PREFLIGHT_MAX_AGE_SECONDS = 600


def is_web_client_route_request(
    chrome_origin: str,
    origin_header: str | None,
    # The path and query of the request being verified, as caddy forwards it.
    forwarded_uri: str,
    is_app_internal: bool,
) -> bool:
    """Whether a request is one the web client's page sent to a workspace route: it names the web client's origin,
    its path is under the workspace routes, and the app it is for is an internal one."""
    if not chrome_origin or origin_header != chrome_origin or not is_app_internal:
        return False
    return forwarded_uri.split("?", 1)[0].startswith(WEB_CLIENT_ROUTE_PATH_PREFIX)
