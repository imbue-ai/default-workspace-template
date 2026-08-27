"""The `mngr forward` instance the flow executor drives the delivered app through.

`mngr forward` is what serves a workspace's apps to the client: it binds a loopback port, routes by
Host header (`<label>.host-<hex>.localhost`), and reaches the workspace over the same per-host SSH
tunnel the eval's exec and rsync already ride. Driving flows through it means the browser sees the
app at the exact origin the client's app tab iframes -- through the product's serving path rather
than under it at a raw in-container socket.

The driver starts its OWN instance rather than discovering the one the headless minds backend may
have spawned: a driver-owned instance has a known port and a known pre-auth token, and does not
couple the eval to backend internals or to a cookie it never minted.

Configured the way minds configures its spawn, so the exercised path is production's. The one
deliberate divergence is `--port`: minds omits it and reads the chosen port back off the readiness
envelope, while a driver-owned instance wants a port it picked, so a conflict is a loud failure
rather than a silent bind somewhere else.
"""

import json
import re
import secrets
import shlex
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure

# Where the instance listens in the box. Deliberately NOT 8421: the headless minds backend may
# already hold that, and an explicit port makes a collision fail loudly instead of landing on an
# OS-assigned port the driver would then have to discover.
FORWARD_PORT: Final[int] = 8431

# The service the bare `host-<hex>` origin resolves to, matching minds. Flows never navigate to the
# bare origin -- they go straight to a delivered app's label -- but the flag is required and the
# value is what decides the shell backend, so it stays at parity.
FORWARD_SHELL_SERVICE: Final[str] = "system_interface"

# The CEL filter minds applies, kept verbatim: it restricts discovery to primary agents, which is
# what a workspace's own apps are registered under.
FORWARD_AGENT_INCLUDE: Final[str] = "has(agent.labels.is_primary)"

# The cookie the proxy gates on. Inlined rather than imported from imbue-mngr-forward, exactly as
# minds inlines it, so the eval does not take a dependency on the plugin's import graph.
SESSION_COOKIE_NAME: Final[str] = "mngr_forward_session"

# What minds mints (`secrets.token_urlsafe(64)`); matched so the token shape is production's.
_SECRET_TOKEN_LENGTH: Final[int] = 64

BOX_FORWARD_LOG_PATH: Final[str] = "/logs/agent/mngr_forward.jsonl"

# Envelope types on the instance's stdout that are worth keeping in the collector's trace.
_LISTENING_EVENT: Final[str] = "listening"
_BACKEND_FAILURE_EVENT: Final[str] = "system_interface_backend_failure"

_HOST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^host-[0-9a-f]{32}$")


def mint_forward_secret() -> str:
    """One secret for the instance, in the shape minds mints.

    Both of the secrets the driver hands the proxy -- the pre-auth cookie and the browser-bridge
    token -- are opaque values the proxy only ever compares against what it was started with, so
    they come from here; they are separate values, and neither is derived from the other.
    """
    return secrets.token_urlsafe(_SECRET_TOKEN_LENGTH)


@pure
def build_forward_command(preauth_cookie: str, browser_bridge_token: str, port: int) -> tuple[str, ...]:
    """The `mngr forward` argv, at flag parity with the spawn in minds' own forward_cli.

    Parity matters because the point of this executor is to exercise the serving path the client
    gets: a differently-configured proxy would be a different path. `--use-http2` in particular is
    the only TLS switch, so dropping it would quietly move flows onto plain HTTP -- which is not
    what any client ever talks to.
    """
    return (
        "forward",
        "--host",
        "127.0.0.1",
        "--service",
        FORWARD_SHELL_SERVICE,
        # Tail the shared discovery log rather than spawning a second `mngr observe`, which is both
        # what minds does and what keeps this instance from doubling discovery load on the box.
        "--observe-via-file",
        "--preauth-cookie",
        preauth_cookie,
        "--browser-bridge-token",
        browser_bridge_token,
        "--format",
        "jsonl",
        # TLS + HTTP/2. Minds passes this so the workspace origin escapes Chromium's per-origin
        # HTTP/1.1 connection cap; here it also means the browser talks https, as a client does.
        "--use-http2",
        "--agent-include",
        FORWARD_AGENT_INCLUDE,
        # The divergence from minds, and the reason for it is in this module's docstring.
        "--port",
        str(port),
    )


# Flags whose VALUE is a secret. The trace ships inside the evidence bundle, so the argv is only ever
# recorded through redact_forward_command, and a new secret-bearing flag has to be added here.
_SECRET_FLAGS: Final[frozenset[str]] = frozenset({"--preauth-cookie", "--browser-bridge-token"})
_REDACTED: Final[str] = "(redacted)"


@pure
def redact_forward_command(argv: tuple[str, ...]) -> str:
    """The instance's argv as the trace records it, with every secret value replaced.

    Keyed on the flags themselves rather than on what a value looks like: a secret is secret because
    of the flag it follows, and a length or shape test would quietly stop covering one the day its
    minting changed.
    """
    parts: list[str] = []
    is_secret_next = False
    for part in argv:
        parts.append(_REDACTED if is_secret_next else part)
        is_secret_next = part in _SECRET_FLAGS
    # The argv opens with the subcommand itself, so this reads as the command line that was run.
    return "mngr {}".format(" ".join(parts))


@pure
def forward_start_command(argv: tuple[str, ...], mngr_dir: str, log_path: str) -> str:
    """The shell that launches the instance in the background and captures its envelopes.

    Backgrounded with setsid because `environment.exec` returns as soon as its command does, and
    the proxy has to outlive that. Its stdout is the envelope stream (readiness, resolver
    snapshots, backend failures) and lands in a file the collector folds into its trace; stderr
    joins it, since the tunnel-setup warnings that separate one backend failure from another are
    only ever logged there.
    """
    quoted = " ".join(shlex.quote(part) for part in argv)
    return "cd {mngr} && setsid nohup uv run mngr {argv} > {log} 2>&1 < /dev/null &".format(
        mngr=mngr_dir, argv=quoted, log=shlex.quote(log_path)
    )


@pure
def forward_probe_command(port: int, preauth_cookie: str, host_id: str) -> str:
    """Probe the instance the way minds does: an HTTP request carrying the session cookie.

    Readiness is NOT the `listening` envelope. That fires from the server's lifespan hook before
    the socket is actually accepting, and even once it accepts, a request only succeeds after
    discovery has resolved the host -- until then the proxy answers 503 with nothing on stdout to
    say so. So the gate is a real request returning 200, exactly as minds gates its own.

    `*.localhost` does not resolve through DNS, so this dials the loopback address and carries the
    origin in the Host header; the browser needs no such help, because it resolves the name itself.
    """
    return (
        "curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 -k "
        "-H {host_header} -H {cookie} https://127.0.0.1:{port}/"
    ).format(
        host_header=shlex.quote("Host: {}.localhost:{}".format(host_id, port)),
        cookie=shlex.quote("Cookie: {}={}".format(SESSION_COOKIE_NAME, preauth_cookie)),
        port=port,
    )


@pure
def forward_stop_command(port: int) -> str:
    """Stop the instance by the port it holds.

    Matched on the exact `--port <n>` argument rather than on the program name, so a `mngr forward`
    the minds backend spawned is never caught by the eval's cleanup.

    The leading dash is written `[-]-` so the pattern cannot match the command line running it:
    `pkill -f` reads every process's argv, including its own shell's, and matching itself would
    take the shell down mid-command.
    """
    return "pkill -f {} || true".format(shlex.quote("mngr forward .*[-]-port {}".format(port)))


@pure
def is_host_id(candidate: str) -> bool:
    """Whether a string is a mngr host id. Checked before it is formatted into an origin, because a
    malformed one produces a URL the proxy silently declines to route rather than an error."""
    return bool(_HOST_ID_PATTERN.match(candidate))


@pure
def forwarded_origin(label: str, host_id: str, port: int) -> str:
    """The URL the client's app tab iframes: the app's own label on the workspace's host origin."""
    return "https://{label}.{host_id}.localhost:{port}/".format(label=label, host_id=host_id, port=port)


@pure
def parse_forward_events(log_text: str) -> tuple[dict[str, Any], ...]:
    """The instance's envelopes, in order. Non-JSON lines (loguru's stderr) are dropped."""
    events: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return tuple(events)


@pure
def summarize_forward_events(events: tuple[dict[str, Any], ...]) -> str:
    """A bounded, judge-free digest of the instance's own account of itself, for the trace.

    Resolver snapshots are dropped: they repeat the full service map on every mutation and would
    swamp the trace. What is kept is the readiness event and every backend failure, which is what
    tells a reader whether the proxy or the workspace leg was the thing that broke.
    """
    lines: list[str] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "")
        if event_type == _LISTENING_EVENT:
            lines.append("listening on {}:{}".format(payload.get("host"), payload.get("port")))
        elif event_type == _BACKEND_FAILURE_EVENT:
            lines.append(
                "backend failure for {}: {} (status {})".format(
                    event.get("agent_id"), payload.get("reason"), payload.get("status_code")
                )
            )
        else:
            # Login URLs and resolver snapshots. The former is a secret and the latter repeats the
            # whole service map on every mutation, so neither belongs in the trace.
            pass
    return "\n".join(lines)
