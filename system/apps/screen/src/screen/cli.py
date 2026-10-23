"""``screen``: drive the Screen from an agent's shell (docs/system/specs/screen/spec.md section 17), thin over the
service like the browser fleet's CLI, with its exit codes: 0 ok, 1 error, 2 preempted (a human has or took control;
end the turn), 3 busy, 4 timed out, 64 usage, 69 no service."""

import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.errors import InvalidScreenSizeError
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER
from screen.primitives import APP_NAME
from screen.primitives import LAUNCH_BROWSER_PATH
from screen.primitives import SERVICE_HOST
from screen.primitives import SERVICE_PORT
from workspace_layout.client import ShellLayoutClient
from workspace_layout.client import requester_from_environment
from workspace_layout.errors import ShellOpError
from workspace_layout.ops import OpenArgs
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import WindowPath
from workspace_layout.shell_url import shell_base_url

DEFAULT_SERVICE_URL: Final[str] = f"http://{SERVICE_HOST}:{SERVICE_PORT}"
ENV_SERVICE_URL: Final[str] = "MINDS_SCREEN_SERVICE_URL"
APPS_FILE: Final[str] = "data/.state/apps.toml"

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
EXIT_PREEMPTED: Final[int] = 2
EXIT_BUSY: Final[int] = 3
EXIT_TIMEOUT: Final[int] = 4
EXIT_USAGE: Final[int] = 64
EXIT_NO_SERVICE: Final[int] = 69

_REQUEST_TIMEOUT_SECONDS: Final[float] = 90.0
_SHELL_OP_TIMEOUT_SECONDS: Final[float] = 30.0
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
HTTP_CONFLICT: Final[int] = 409


class ServiceAnswer(FrozenModel):
    """One answer of the service: the status and the JSON body (``{}`` for a non-JSON body)."""

    status: int = Field(description="The HTTP status")
    body: dict[str, Any] = Field(description="The parsed JSON body")
    raw: bytes = Field(default=b"", description="The raw body, for an image")


def _out(message: str) -> None:
    click.echo(message)


def _err(message: str) -> None:
    click.echo(message, err=True)


def _repo_root() -> Path:
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "system" / "scripts" / "forward_port.py").exists():
            return candidate
    return here


def service_url() -> str:
    """The service's base URL: the environment's override, the app registry's row, else loopback 8090."""
    override = os.environ.get(ENV_SERVICE_URL)
    if override:
        return override.rstrip("/")
    registry = Path(os.environ.get("MINDS_APPS_FILE", APPS_FILE))
    if not registry.is_absolute():
        registry = _repo_root() / registry
    try:
        document = tomllib.loads(registry.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.debug("Could not read the app registry at {} ({}); using {}", registry, e, DEFAULT_SERVICE_URL)
        return DEFAULT_SERVICE_URL
    for app in document.get("apps", []):
        if app.get("name") == str(APP_NAME) and app.get("url"):
            return str(app["url"]).rstrip("/")
    return DEFAULT_SERVICE_URL


@pure
def own_chat_id() -> str | None:
    """The caller's chat id: ``MINDS_CHAT_ID`` on a chat's agent, else the agent's own id, else None."""
    return os.environ.get("MINDS_CHAT_ID") or os.environ.get("MNGR_AGENT_ID")


def _headers(is_identity_required: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    chat_id = own_chat_id()
    if chat_id:
        headers[AGENT_ID_HEADER] = chat_id
        name = os.environ.get("MNGR_AGENT_NAME")
        if name:
            headers[AGENT_NAME_HEADER] = name
    elif is_identity_required:
        _err("MNGR_AGENT_ID is not set -- run `screen control ...` from inside an agent.")
        raise SystemExit(EXIT_USAGE)
    return headers


def request_service(method: str, path: str, body: dict[str, Any] | None, is_identity_required: bool) -> ServiceAnswer:
    url = service_url() + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=_headers(is_identity_required))
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return ServiceAnswer(
                status=response.status, body=parse_answer_body(raw, response.headers.get("Content-Type", "")), raw=raw
            )
    except urllib.error.HTTPError as e:
        raw = e.read()
        return ServiceAnswer(status=e.code, body=parse_answer_body(raw, e.headers.get("Content-Type", "")), raw=raw)
    except urllib.error.URLError as e:
        _err(f"cannot reach the Screen service at {service_url()} ({e.reason}). Is it running?")
        raise SystemExit(EXIT_NO_SERVICE) from e


@pure
def parse_answer_body(raw: bytes, content_type: str) -> dict[str, Any]:
    """The JSON object a service answer carries; ``{}`` for an image or any other non-JSON body."""
    if "json" not in content_type.lower():
        return {}
    try:
        parsed = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@pure
def _detail(answer: ServiceAnswer, fallback: str) -> str:
    return str(answer.body.get("detail") or answer.body.get("error") or fallback)


@pure
def describe_controller(control: dict[str, Any], me: str | None) -> str:
    controller = control.get("controller")
    if controller == "agent":
        if control.get("agent_id") == me:
            return "you"
        return f"agent {control.get('agent_name') or control.get('agent_id') or '?'}"
    if controller == "human":
        return f"the human ({control.get('human_name') or 'someone'})"
    return "nobody"


@pure
def render_acquire_answer(body: dict[str, Any], me: str | None) -> tuple[str, int]:
    """The line to print and the exit code for an ``acquire`` answer, the fleet CLI's rules."""
    if body.get("ok"):
        return "acquired the Screen", EXIT_OK
    status = body.get("status")
    enqueued = bool(body.get("enqueued"))
    if status == "busy_human":
        if enqueued:
            return (
                "the human has control of the Screen -- you're queued to resume and will be messaged when they "
                "hand it back (if you don't hear back in a while, re-run `screen control acquire`). Tell the user, "
                "then end your turn.",
                EXIT_PREEMPTED,
            )
        return "the human has control of the Screen right now.", EXIT_BUSY
    if status == "busy_agent":
        who = describe_controller(body, me)
        return (
            f"the Screen is held by {who} -- you're queued and will be messaged when it frees (if you don't hear "
            "back in a while, re-run `screen control acquire`).",
            EXIT_BUSY,
        )
    if status == "starting":
        return "the Screen is starting up -- try again in a few seconds.", EXIT_BUSY
    if status == "timed_out":
        return "the Screen stayed busy; gave up.", EXIT_TIMEOUT
    return str(body.get("detail") or f"acquire failed ({status})"), EXIT_ERROR


@click.group()
def main() -> None:
    """Drive the Screen: run programs on it, list and raise its windows, take screenshots, hold the control lease."""
    # The command reports everything it has to say itself; the layout client's own logs would add lines to stderr
    # that no agent reading this output expects.
    logger.remove()


@main.command()
@click.argument("command", nargs=-1, required=True)
def run(command: tuple[str, ...]) -> None:
    """Start COMMAND on the Screen (with DISPLAY=:99, detached) and print its pid."""
    answer = request_service("POST", "/api/run", {"command": list(command)}, is_identity_required=False)
    if answer.status == HTTP_SERVICE_UNAVAILABLE:
        _err(_detail(answer, "the Screen is still starting; try again in a moment"))
        raise SystemExit(EXIT_BUSY)
    if answer.status != 200:
        _err(_detail(answer, f"run failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    _out(f"started {command[0]} on the Screen (pid {answer.body.get('pid')})")


@main.command()
def windows() -> None:
    """List the X windows on the Screen: id, class, geometry, title."""
    answer = request_service("GET", "/api/windows", None, is_identity_required=False)
    if answer.status == HTTP_CONFLICT:
        _err(_detail(answer, "the Screen is not running"))
        raise SystemExit(EXIT_BUSY)
    if answer.status != 200:
        _err(_detail(answer, f"windows failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    listed = answer.body.get("windows", [])
    if not listed:
        _out("no windows on the Screen")
        return
    for window in listed:
        geometry = f"{window.get('width')}x{window.get('height')}+{window.get('x')}+{window.get('y')}"
        _out(f"{window.get('id')}  {window.get('window_class')}  {geometry}  {window.get('title')}")


@main.command()
@click.argument("window_id")
def activate(window_id: str) -> None:
    """Raise and focus a window by its id (from `screen windows`)."""
    answer = request_service("POST", f"/api/windows/{window_id}/activate", {}, is_identity_required=False)
    if answer.status != 200:
        _err(_detail(answer, f"activate failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    _out(f"activated {window_id}")


@main.command()
@click.option(
    "--quality", type=click.IntRange(1, 100), default=90, show_default=True, help="JPEG quality neko captures at"
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write; PNG unless the name ends in .jpg or .jpeg",
)
def shot(quality: int, out_path: Path | None) -> None:
    """Take a screenshot of the Screen (PNG by default) and print its path."""
    target = out_path if out_path is not None else Path(f"/tmp/screen-shot-{int(time.time())}.png")
    image_format = "jpeg" if target.suffix.lower() in (".jpg", ".jpeg") else "png"
    answer = request_service(
        "GET", f"/api/screenshot?quality={quality}&format={image_format}", None, is_identity_required=False
    )
    if answer.status == HTTP_CONFLICT:
        _err(_detail(answer, "the Screen is not running"))
        raise SystemExit(EXIT_BUSY)
    if answer.status != 200:
        _err(_detail(answer, f"shot failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(answer.raw)
    _out(str(target))


@pure
def parse_size_argument(value: str) -> tuple[int, int]:
    """``WxH`` as the command takes it."""
    width_text, separator, height_text = value.lower().partition("x")
    if not separator or not width_text.isdigit() or not height_text.isdigit():
        raise InvalidScreenSizeError(f"a size is WIDTHxHEIGHT, such as 1280x720, not {value!r}")
    return int(width_text), int(height_text)


@main.command()
@click.argument("size", required=False)
def size(size: str | None) -> None:
    """Print the Screen's size (WxH@rate), or set it to SIZE (WxH; rounded down to 16 px, within 640x480 and 1920x1080)."""
    if size is not None:
        try:
            width, height = parse_size_argument(size)
        except InvalidScreenSizeError as e:
            _err(str(e))
            raise SystemExit(EXIT_USAGE) from e
        answer = request_service(
            "POST", "/api/screen-size", {"width": width, "height": height}, is_identity_required=False
        )
        if answer.status == HTTP_CONFLICT:
            _err(_detail(answer, "the Screen is not running"))
            raise SystemExit(EXIT_BUSY)
        if answer.status != 200:
            _err(_detail(answer, f"size failed ({answer.status})"))
            raise SystemExit(EXIT_ERROR)
        applied = answer.body.get("screen_size")
        if not isinstance(applied, dict):
            _err("the service answered no size")
            raise SystemExit(EXIT_ERROR)
        _out(f"{applied.get('width')}x{applied.get('height')}@{applied.get('rate')}")
        return
    answer = request_service("GET", "/api/state", None, is_identity_required=False)
    if answer.status != 200:
        _err(_detail(answer, f"state failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    screen_size = answer.body.get("screen_size")
    if not isinstance(screen_size, dict):
        _err(f"the Screen is {answer.body.get('lifecycle')}; it has no size yet")
        raise SystemExit(EXIT_BUSY)
    _out(f"{screen_size.get('width')}x{screen_size.get('height')}@{screen_size.get('rate')}")


@main.group()
def browser() -> None:
    """The browser on the Screen: one Chromium with the fleet's profile (the agentic-browser-fleet command drives it)."""


@browser.command("open")
@click.argument("url", required=False)
def browser_open(url: str | None) -> None:
    """Open the browser on the Screen (starting the Screen and the browser when needed), with URL as a new tab in front
    when given. What the XFCE session's Web Browser launcher and an http(s) link opened on the Screen run."""
    body: dict[str, Any] = {"url": url} if url is not None else {}
    answer = request_service("POST", LAUNCH_BROWSER_PATH, body, is_identity_required=False)
    if answer.status == HTTP_SERVICE_UNAVAILABLE:
        _err(_detail(answer, "the browser is still installing; try again in a moment"))
        raise SystemExit(EXIT_BUSY)
    if answer.status != 200:
        _err(_detail(answer, f"browser open failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    _out("opened the browser on the Screen" + (f" at {url}" if url else ""))


@main.group()
def control() -> None:
    """The control lease: acquire it before driving, release it when done, hand off for a CAPTCHA."""


@control.command("acquire")
@click.option(
    "--reclaim",
    is_flag=True,
    default=False,
    help="Take the Screen back from the human -- ONLY when they told you to continue.",
)
def control_acquire(reclaim: bool) -> None:
    """Acquire the lease (starts the Screen when it is down)."""
    answer = request_service("POST", "/api/control/acquire", {"reclaim": reclaim}, is_identity_required=True)
    if answer.status != 200:
        _err(_detail(answer, f"acquire failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    line, code = render_acquire_answer(answer.body, own_chat_id())
    (_out if code in (EXIT_OK, EXIT_PREEMPTED) else _err)(line)
    raise SystemExit(code)


@control.command("release")
def control_release() -> None:
    """Release the lease you hold."""
    answer = request_service("POST", "/api/control/release", {}, is_identity_required=True)
    if answer.status != 200:
        _err(_detail(answer, f"release failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    _out("released the Screen" if answer.body.get("released") else "the Screen was not yours to release")


@control.command("handoff")
@click.argument("reason", required=False, default="human verification needed")
def control_handoff(reason: str) -> None:
    """Hand the Screen to the human for something you cannot do, then stop."""
    answer = request_service("POST", "/api/control/handoff", {"reason": reason}, is_identity_required=True)
    if answer.status != 200:
        _err(_detail(answer, f"handoff failed ({answer.status})"))
        raise SystemExit(EXIT_ERROR)
    if answer.body.get("ok"):
        # The lease is the human's whether or not the window could be shown; the agent must hear that either way.
        is_shown = open_screen_window(is_minimized=False)
        _out(
            f"handed the Screen to the human: {reason}. You're first in line to resume. Tell the user what to do"
            + ("" if is_shown else " (and to open the Screen themselves, since its window could not be shown)")
            + ", end your turn, and re-run `screen control acquire` when they hand control back."
        )
        raise SystemExit(EXIT_PREEMPTED)
    _out(
        "the Screen isn't yours to hand off -- a human may already control it. Re-run `screen control acquire` to see."
    )
    raise SystemExit(EXIT_PREEMPTED)


def open_screen_window(is_minimized: bool) -> bool:
    """Open the Screen's window for the user through the shell's ``open`` op (one window, focused if present);
    answers whether it was opened, with the shell's refusal printed to stderr when it was not."""
    shell = ShellLayoutClient(
        shell_url=shell_base_url(), requester=requester_from_environment(), timeout_seconds=_SHELL_OP_TIMEOUT_SECONDS
    )
    try:
        shell.open(OpenArgs(app=APP_NAME, path=WindowPath("/"), if_present=IfPresent.FOCUS, minimized=is_minimized))
    except ShellOpError as e:
        _err(f"the Screen's window could not be opened: {e}")
        return False
    return True


@main.command("open")
@click.option(
    "--show",
    "is_shown",
    is_flag=True,
    default=False,
    help="Restore and raise the window rather than opening it minimized.",
)
def open_command(is_shown: bool) -> None:
    """Open the Screen's window on the user's desktop (minimized unless --show)."""
    if not open_screen_window(is_minimized=not is_shown):
        raise SystemExit(EXIT_ERROR)
    _out("opened the Screen's window")


if __name__ == "__main__":
    main()
