"""The forward instance the flow executor serves the delivered app through.

The parity test here is the load-bearing one: this executor's whole claim is that it exercises the
serving path the client gets, and a differently-configured proxy would be a different path. It
asserts against minds' own argv builder rather than a copied list, so the two cannot drift apart
without a test noticing.
"""

import ast
import re
from pathlib import Path

import pytest

from imbue.minds_evals import forward_instance

# .../apps/minds_evals/imbue/minds_evals/<this file>
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MINDS_FORWARD_CLI = _REPO_ROOT / "apps" / "minds" / "imbue" / "minds" / "desktop_client" / "forward_cli.py"


def _minds_forward_flags() -> set[str]:
    """Every flag minds' own spawn passes, read out of the function that builds it.

    Read from the source rather than imported: apps/minds pulls in Electron-adjacent machinery this
    project has no reason to depend on, and the argv is a literal list this can read exactly.
    """
    tree = ast.parse(_MINDS_FORWARD_CLI.read_text())
    builder = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_build_forward_command"
    )
    return {
        node.value
        for node in ast.walk(builder)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("--")
    }


def _eval_flags() -> set[str]:
    argv = forward_instance.build_forward_command("cookie", "bridge", forward_instance.FORWARD_PORT)
    return {part for part in argv if part.startswith("--")}


# Flags minds passes that shape only how the app is EMBEDDED, not how it is served.
#
# --embedder-origin writes the `frame-ancestors` CSP that says who may iframe the app, and minds
# names its own Electron shell's origins. The origin surface navigates straight to the app, so
# nothing is framing it and there is no shell to name. --reverse threads reverse tunnels minds
# never actually configures (its default is empty, so the flag is never emitted at all).
#
# If the reserved `minds-ui` surface ever lands, it reaches the app AS an iframe, and
# --embedder-origin moves from irrelevant to required. That is the moment to revisit this.
_EMBEDDING_ONLY_FLAGS = {"--embedder-origin", "--reverse"}


def test_the_forward_instance_matches_every_serving_flag_minds_spawns_with() -> None:
    # This executor's claim is that it exercises the serving path the client gets, so every flag
    # that shapes that path is matched. `--use-http2` matters most: it is the only TLS switch, so
    # dropping it would quietly move flows onto plain HTTP, which no client ever talks to.
    missing = _minds_forward_flags() - _eval_flags() - _EMBEDDING_ONLY_FLAGS

    assert missing == set(), "the eval's forward instance is missing serving flags minds passes: {}".format(missing)


def test_the_forward_instance_adds_only_a_chosen_port() -> None:
    # The one deliberate addition: minds omits --port and reads the chosen one back off the
    # readiness envelope, while a driver-owned instance wants a port it picked, so a collision is a
    # loud failure instead of a silent bind somewhere else.
    assert _eval_flags() - _minds_forward_flags() == {"--port"}


def test_the_only_flags_minds_passes_and_this_does_not_are_about_embedding() -> None:
    # Pinned so a NEW minds flag cannot quietly join the excused set: anything else appearing here
    # is a serving-path divergence and should fail the parity test above.
    assert _minds_forward_flags() - _eval_flags() == _EMBEDDING_ONLY_FLAGS


def test_the_forward_command_carries_tls_and_the_shell_service() -> None:
    argv = forward_instance.build_forward_command("cookie", "bridge", 8431)

    assert argv[0] == "forward"
    assert "--use-http2" in argv
    assert argv[argv.index("--service") + 1] == forward_instance.FORWARD_SHELL_SERVICE
    assert argv[argv.index("--port") + 1] == "8431"


def test_the_forward_command_does_not_take_the_port_minds_uses() -> None:
    # A headless minds backend on the same box holds 8421; colliding with it would make the eval's
    # own instance either fail or, worse, land somewhere it never told anyone about.
    assert forward_instance.FORWARD_PORT != 8421


def test_the_traced_command_redacts_every_secret_argument() -> None:
    # The trace ships inside the evidence bundle. Redaction is keyed on the flag, not on how long or
    # how random the value looks, so a short secret is covered exactly like a long one.
    traced = forward_instance.redact_forward_command(forward_instance.build_forward_command("s3cret", "brdg", 8431))

    assert "s3cret" not in traced and "brdg" not in traced
    assert "--preauth-cookie (redacted)" in traced and "--browser-bridge-token (redacted)" in traced
    # Everything that is not a secret still reads as the command line that ran.
    assert traced.startswith("mngr forward ") and "--port 8431" in traced


def test_stopping_the_instance_cannot_catch_the_backends_own() -> None:
    # Matched on the exact --port argument, not the program name.
    stop = forward_instance.forward_stop_command(8431)

    assert "[-]-port 8431" in stop
    assert "8421" not in stop
    # `pkill -f` matches against every process's argv, its own shell's included, so a pattern that
    # matched this very command would kill the shell instead of the proxy.
    assert not re.search("mngr forward .*[-]-port 8431", stop)


# --- the forwarded origin ---


def test_the_forwarded_origin_is_built_from_the_rows_label() -> None:
    # The label is the unguessable `<name>-<rand>` component forward_port.py mints and the proxy
    # routes on, mapping it back to the service itself -- so a URL must carry the label, not the name.
    origin = forward_instance.forwarded_origin("todo-3ijqotwh", "host-" + "a" * 32, 8431)

    assert origin == "https://todo-3ijqotwh.host-{}.localhost:8431/".format("a" * 32)


@pytest.mark.parametrize(
    "candidate,is_valid",
    [
        ("host-" + "0123456789abcdef" * 2, True),
        # An agent id is a different uuid4 entirely and never routes.
        ("agent-72fdb07576b94736828925a3251f1b13", False),
        ("host-tooshort", False),
        ("", False),
    ],
)
def test_only_a_real_host_id_is_formatted_into_an_origin(candidate: str, is_valid: bool) -> None:
    # A malformed host id produces a URL the proxy silently declines to route rather than an error,
    # so it is checked before it ever reaches a browser.
    assert forward_instance.is_host_id(candidate) is is_valid


def test_the_readiness_probe_carries_the_session_cookie_and_the_origin_by_header() -> None:
    # `*.localhost` does not resolve through DNS, so the probe dials loopback and names the origin
    # in a header; the browser needs no such help because it resolves the name itself.
    probe = forward_instance.forward_probe_command(8431, "tok", "host-" + "b" * 32)

    assert "https://127.0.0.1:8431/" in probe
    assert "Host: host-{}.localhost:8431".format("b" * 32) in probe
    assert "{}=tok".format(forward_instance.SESSION_COOKIE_NAME) in probe
    # The proxy's leaf is self-signed local machinery, not the thing under test.
    assert " -k " in probe


# --- the instance's own account of itself ---


def test_forward_events_keep_readiness_and_backend_failures() -> None:
    # These are what separate a dead proxy from a dead tunnel after the fact, without re-running.
    log_text = "\n".join(
        [
            "not json at all, a loguru line",
            '{"stream":"forward","payload":{"type":"login_url","url":"https://x/login"}}',
            '{"stream":"forward","payload":{"type":"listening","host":"127.0.0.1","port":8431}}',
            '{"stream":"forward","payload":{"type":"resolver_snapshot","services_by_agent":{}}}',
            '{"stream":"forward","agent_id":"agent-1",'
            '"payload":{"type":"system_interface_backend_failure","reason":"CONNECT_ERROR","status_code":503}}',
        ]
    )

    summary = forward_instance.summarize_forward_events(forward_instance.parse_forward_events(log_text))

    assert "listening on 127.0.0.1:8431" in summary
    assert "backend failure for agent-1: CONNECT_ERROR (status 503)" in summary
    # Resolver snapshots repeat the whole service map on every mutation and would swamp the trace.
    assert "resolver_snapshot" not in summary


def test_a_minted_secret_is_the_shape_minds_mints() -> None:
    token = forward_instance.mint_forward_secret()

    assert len(token) >= 64
    assert token != forward_instance.mint_forward_secret()
