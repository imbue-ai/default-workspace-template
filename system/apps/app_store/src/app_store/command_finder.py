"""Working out an added app's start command, so the person adding it never has to.

Given only a package (and its source, version and display name), a job in a background thread:

1. pins the version and checks the package fits, as "Check" does;
2. installs it into a scratch folder of its own (``.command-trials/<job>/`` in the store's data folder), so nothing
   of the real install is touched;
3. lists the programs it installed (none: it is a library, not an app) and reads each one's own ``--help`` and the
   package's description on its registry;
4. asks Claude for the command, as JSON that also says when the package is not an app with a window at all, or
   needs setting up (an account, a key, a config file) before it can start;
5. checks the command against the same rules a typed one is held to, runs it on a free port, and waits for a web page
   to answer on ``/``. A page that forbids being framed (X-Frame-Options, or a frame-ancestors policy without ``*``)
   counts as a failure, since the workspace shows every app framed and its window would be blank. While a page
   answers, a screenshot is taken for the person to judge. If it does not answer, its own
   output goes back to Claude with the command that failed, and a new command is tried, up to ``MAX_ATTEMPTS``.

Every outcome is a plain sentence for someone who knows nothing about the package; the scratch folder is always
deleted, and the job's raw log (every command run, its output, and Claude's answers) is kept beside it.

The found command is only a suggestion: the dialog fills it in, and the person still checks and confirms the add as
they would a typed one.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Final
from typing import Literal

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from app_store.added_apps import AddAppRequest
from app_store.added_apps import CheckedRequest
from app_store.added_apps import SOURCE_NAMES
from app_store.added_apps import SetupFileText
from app_store.added_apps import check_command
from app_store.added_apps import check_package_request
from app_store.added_apps import check_setup_files
from app_store.added_apps import taken_app_names
from app_store.adding import recipe_for
from app_store.catalog import CatalogApp
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonRecipe
from app_store.catalog import SetupFile
from app_store.catalog import render_argv
from app_store.catalog import render_setup_content
from app_store.claude_p import ClaudeCLIError
from app_store.claude_p import claude_p_completion
from app_store.engine import InstallEngine
from app_store.engine import installed_programs
from app_store.errors import AddAppRefusedError
from app_store.errors import InstallError
from app_store.errors import PackageLookupError
from app_store.errors import PortError
from app_store.layout import NO_BACKUP_MARKER
from app_store.package_check import NPM_REGISTRY_ROOT
from app_store.package_check import PYPI_ROOT
from app_store.package_check import PackageChecker
from app_store.package_check import RegistryClientInterface
from app_store.ports import pick_free_port
from app_store.records import now_iso
from app_store.room import RoomVerdict
from app_store.room import check_room
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# The model that writes the command: set here, independent of any chat's model.
FIND_MODEL: Final[str] = "claude-sonnet-5-5"
MAX_ATTEMPTS: Final[int] = 3
# How long one answer may take before the AI counts as unreachable, so a stuck call never leaves the dialog waiting.
FIND_TIMEOUT_SECONDS: Final[float] = 300.0
TRIALS_DIRNAME: Final[str] = ".command-trials"
# A test launch's own output, kept beside the job's log while it runs.
TRIAL_OUTPUT_SUFFIX: Final[str] = ".out"
# How many jobs are kept, in memory and as logs on disk; older ones are dropped as new ones start.
MAX_KEPT_JOBS: Final[int] = 20
# How long a test launch has to answer with a page.
TRIAL_START_TIMEOUT_SECONDS: Final[float] = 60.0
TRIAL_PROBE_INTERVAL_SECONDS: Final[float] = 0.5
TRIAL_STOP_GRACE_SECONDS: Final[float] = 5.0
HELP_TIMEOUT_SECONDS: Final[float] = 15.0
SCREENSHOT_TIMEOUT_SECONDS: Final[float] = 60.0
# How much of each thing Claude is shown.
MAX_PROGRAMS_READ: Final[int] = 6
HELP_CHARACTERS: Final[int] = 5000
README_CHARACTERS: Final[int] = 12000
OUTPUT_TAIL_LINES: Final[int] = 40
SCREENSHOT_SCRIPT: Final[Path] = Path(__file__).parent / "screenshot_page.py"

_SYSTEM_PROMPT: Final[str] = """\
You work out the command that starts a web app installed from a package registry, for an app store in a person's \
private workspace. The person is not technical. The app runs on a server and is shown to them in a window, through \
the workspace's own proxy, so it must serve a web page meant for people at / on the port it is given.

Answer with one JSON object and nothing else:
{"kind": "web_app" | "not_an_app" | "needs_setup", "command": "...", "files": [{"path": "...", "content": "..."}], \
"explanation": "..."}

- "web_app": the package can serve such a page. "command" is the full command line. "files" is optional: settings \
files the store writes into the app's own folder before starting it, for an app whose options (such as allowing \
framing) can only be set in a file. Give each a plain relative "path" (like "config.yaml") and its full "content"; \
the command can name it as {data_dir}/config.yaml. In a file, {port} and {data_dir} are filled in and every other \
brace is left as written. Keep files short and use them only when the command alone cannot do the job. Files are \
settings only (.yaml, .yml, .json, .toml, .ini, .cfg, .conf, .env, .properties, .txt): never code, scripts or \
plugins. If the app can only be made to allow framing by writing code, answer needs_setup.
- "not_an_app": the package is a library, a command-line tool, or anything else with no web page to show. "command" \
is "".
- "needs_setup": it is a web app, but it cannot start until the person does something first (creates an account, \
gets an API key, runs a database). A settings file you can write yourself is never a reason for needs_setup: write \
it in "files". "command" is "".

"explanation" is one or two short sentences in plain words for the person: for web_app, what the app is for; for \
not_an_app, what the package is instead; for needs_setup, exactly what they must get or do first. Never name flags, \
files or code in it.

Rules for the command, all required:
- It starts with one of the programs the package installed, by its bare name, exactly as listed.
- It passes the port as the literal text {port} and makes the app listen on 127.0.0.1 only.
- It may use {data_dir} for a folder of the app's own where it can keep its files; it runs in that folder.
- No shell runs it: no pipes, redirects, &&, environment variable assignments or $VARIABLES. Quotes group words.
- {port} and {data_dir} are the only placeholders. Write every other brace twice: a JSON argument such as \
{"a": 1} is written {{"a": 1}}.
- It runs unattended: no prompts, never opens a browser, no reloader or watch mode, no debug mode.
- The page is reached through a proxy on another hostname and is shown inside an iframe on another site, so if the \
app has options to allow any host or origin, turn them on. It must not forbid being framed: many apps send \
X-Frame-Options or a Content-Security-Policy frame-ancestors by default, so when the app has an option to set its \
headers or its frame-ancestors policy, make it allow any frame ancestor (*). The workspace already requires the person to sign in, so \
turn off the app's own login token or password when it has an option for that.
- Prefer the app's normal, ordinary way of running over a demo, unless the package itself is a demo.

When earlier tries are listed, they failed: read the output, fix the cause, and do not repeat a command that failed. \
If the output shows the app needs something only the person can provide, answer needs_setup."""


class FinderStatus(StrEnum):
    """Where a job stands."""

    WORKING = "WORKING"
    FOUND = "FOUND"
    NOT_AN_APP = "NOT_AN_APP"
    NEEDS_SETUP = "NEEDS_SETUP"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FinderAttempt(FrozenModel):
    """One command that was tried."""

    command: str = Field(description="The command as Claude wrote it")
    outcome: str = Field(description="What happened, in plain words")


class FinderJob(FrozenModel):
    """A job working out an app's start command, as the dialog polls it."""

    job_id: str = Field(description="The job's id")
    status: FinderStatus = Field(description="Where it stands")
    progress: str = Field(description="What it is doing now, in plain words; empty once it is finished")
    package: str = Field(description="The package being worked out")
    version: str = Field(description="The version pinned; empty until it is looked up")
    command: str = Field(description="The start command found; empty unless FOUND")
    setup_files: tuple[SetupFile, ...] = Field(
        default=(), description="The settings files it starts with; empty unless FOUND with files"
    )
    explanation: str = Field(description="For the person: what the app is for, what it is instead, or what went wrong")
    attempts: tuple[FinderAttempt, ...] = Field(description="Every command tried, in order")
    has_screenshot: bool = Field(description="Whether a screenshot of the working app was taken")
    started_at: str = Field(description="When it started, ISO 8601 UTC")


class Proposal(FrozenModel):
    """What Claude answers; any other keys it adds are ignored."""

    model_config = {**FrozenModel.model_config, "extra": "ignore"}

    kind: Literal["web_app", "not_an_app", "needs_setup"] = Field(description="What the package is")
    command: str = Field(default="", description="The command, for a web app")
    files: tuple[SetupFileText, ...] = Field(default=(), description="Settings files to write before starting it")
    explanation: str = Field(description="For the person, in plain words")


class PackageContext(FrozenModel):
    """What Claude is shown about the package."""

    registry: str = Field(description="PyPI or npm")
    package: str = Field(description="The package's name")
    version: str = Field(description="The pinned version")
    summary: str = Field(description="Its one-line description")
    programs: tuple[str, ...] = Field(description="The programs it installed")
    help_texts: dict[str, str] = Field(description="Each program's own --help output, trimmed")
    readme: str = Field(description="Its description on the registry, trimmed")


class FailedTry(FrozenModel):
    """A command that failed, with what Claude is told about why."""

    command: str = Field(description="The command")
    what_happened: str = Field(description="The failure, with the app's own output")


class CommandWriterInterface(MutableModel, ABC):
    """Writes a start command from what is known about the package."""

    @abstractmethod
    def propose(self, context: PackageContext, failed: list[FailedTry], log_path: Path) -> Proposal:
        """Claude's answer; raises FinderError when no usable answer came back (written to the log)."""


class FinderError(Exception):
    """Working out the command could not go on; the message is for the person."""


class UnreadableAnswerError(FinderError):
    """Claude's answer held no usable JSON object; the next try is asked again."""


class ClaudeCommandWriter(CommandWriterInterface):
    """The production writer: one ``claude -p`` completion per try, no tools."""

    model: str = Field(default=FIND_MODEL, frozen=True, description="The model asked")
    timeout_seconds: float = Field(
        default=FIND_TIMEOUT_SECONDS, frozen=True, description="How long an answer may take"
    )

    def propose(self, context: PackageContext, failed: list[FailedTry], log_path: Path) -> Proposal:
        prompt = render_prompt(context, failed)
        try:
            result = claude_p_completion(
                prompt, system=_SYSTEM_PROMPT, model=self.model, timeout_seconds=self.timeout_seconds
            )
        except ClaudeCLIError as e:
            _append(log_path, f"[app-store] the AI could not be reached: {e}")
            raise FinderError(
                "The AI that works out start commands couldn't be reached. Try again in a moment."
            ) from e
        _append(log_path, f"[app-store] the AI answered (${result.cost_usd:.4f}):\n{result.text}")
        return parse_proposal(result.text)


def render_prompt(context: PackageContext, failed: list[FailedTry]) -> str:
    """The prompt for one try."""
    parts = [
        f"Package: {context.package} {context.version}, from {context.registry}.",
        f"Its own description: {context.summary or '(none)'}",
        f"Programs it installed: {', '.join(context.programs)}",
    ]
    for program, text in context.help_texts.items():
        parts.append(f"--- {program} --help ---\n{text or '(printed nothing)'}")
    parts.append(f"--- its description on {context.registry} ---\n{context.readme or '(none)'}")
    for number, attempt in enumerate(failed, start=1):
        parts.append(f"--- try {number} failed ---\nCommand: {attempt.command}\n{attempt.what_happened}")
    return "\n\n".join(parts)


def parse_proposal(text: str) -> Proposal:
    """The JSON object in Claude's answer; raises FinderError when there is none."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise UnreadableAnswerError("The AI's answer couldn't be read.")
    try:
        return Proposal.model_validate(json.loads(text[start : end + 1]))
    except (ValueError, ValidationError) as e:
        raise UnreadableAnswerError("The AI's answer couldn't be read.") from e


class TrialOutcome(FrozenModel):
    """How one test launch went."""

    is_answering: bool = Field(description="Whether a page answered on its port")
    what_happened: str = Field(description="For the person: why it did not work; empty when it did")
    output_tail: str = Field(description="The last of the program's own output")
    screenshot: bytes | None = Field(default=None, description="A PNG of the page while it answered")


class TrialRunnerInterface(MutableModel, ABC):
    """Test-launches a command and stops it again."""

    @abstractmethod
    def run(self, argv: list[str], cwd: Path, port: int, log_path: Path, cancelled: threading.Event) -> TrialOutcome:
        """Run ``argv`` until a page answers on ``port`` (screenshot it), it exits, or the time runs out; always stop
        it before returning."""


class ProcessTrialRunner(TrialRunnerInterface):
    """The production runner: a process group of its own, probed over HTTP, screenshot through the workspace's
    browser."""

    repo_root: Path = Field(frozen=True, description="Where the workspace's own Python environment is")
    start_timeout_seconds: float = Field(default=TRIAL_START_TIMEOUT_SECONDS, frozen=True, description="Start wait")

    def run(self, argv: list[str], cwd: Path, port: int, log_path: Path, cancelled: threading.Event) -> TrialOutcome:
        cwd.mkdir(parents=True, exist_ok=True)
        output_path = log_path.with_suffix(f".try-{port}{TRIAL_OUTPUT_SUFFIX}")
        _append(log_path, f"$ {shlex.join(argv)}")
        with output_path.open("w", encoding="utf-8") as output:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                return TrialOutcome(is_answering=False, what_happened=f"It couldn't be started ({e}).", output_tail="")
            try:
                outcome, headers = self._watch(process, port, cancelled)
                refusal = framing_refusal(headers) if outcome == "answering" else None
                screenshot = (
                    take_screenshot(self.repo_root, port) if outcome == "answering" and refusal is None else None
                )
                if screenshot is not None and screenshot.blocked_reason is not None:
                    refusal = f"a browser refusing to show it in a frame: {screenshot.blocked_reason}"
                if refusal is not None:
                    outcome = "refuses framing"
            finally:
                _stop_group(process)
        tail = _tail(output_path)
        _append(log_path, f"[app-store] its output:\n{tail}")
        output_path.unlink(missing_ok=True)
        match outcome:
            case "answering":
                image = screenshot.image if screenshot is not None else None
                return TrialOutcome(is_answering=True, what_happened="", output_tail=tail, screenshot=image)
            case "exited":
                what = f"It stopped right away (exit status {process.returncode})."
            case "cancelled":
                what = "It was cancelled."
            case "refuses framing":
                what = "It started, but it refuses to be shown inside a window, so its window would be blank."
                return TrialOutcome(
                    is_answering=False,
                    what_happened=what,
                    output_tail=f"Shown inside another page, it was blocked ({refusal}). "
                    "Find the app's option that sets that header (or its content security policy) and allow any "
                    f"frame ancestor.\n{tail}",
                )
            case _:
                what = f"It kept running but never showed a page within {self.start_timeout_seconds:.0f} seconds."
        return TrialOutcome(is_answering=False, what_happened=what, output_tail=tail)

    def _watch(
        self, process: subprocess.Popen[bytes], port: int, cancelled: threading.Event
    ) -> tuple[str, httpx.Headers]:
        """How the launch went, and the headers of the page that answered (after its redirects)."""
        deadline = time.monotonic() + self.start_timeout_seconds
        while time.monotonic() < deadline:
            if cancelled.is_set():
                return "cancelled", httpx.Headers()
            if process.poll() is not None:
                return "exited", httpx.Headers()
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0, follow_redirects=True)
                if response.status_code < 500:
                    return "answering", response.headers
            except httpx.HTTPError:
                pass
            cancelled.wait(TRIAL_PROBE_INTERVAL_SECONDS)
        return "timed out", httpx.Headers()


def framing_refusal(headers: httpx.Headers) -> str | None:
    """The header by which a page forbids being shown inside another site's page -- which is how the workspace shows
    every app, so such an app's window is blank -- or None when it allows it."""
    frame_options = headers.get("x-frame-options", "").strip()
    if frame_options != "":
        return f"X-Frame-Options: {frame_options}"
    policy = headers.get("content-security-policy", "")
    for directive in policy.split(";"):
        words = directive.split()
        if words and words[0].lower() == "frame-ancestors" and "*" not in words[1:]:
            return f"Content-Security-Policy: {directive.strip()}"
    return None


class Screenshot(FrozenModel):
    """A picture of the app as its window would show it."""

    image: bytes | None = Field(description="The PNG; None when no picture could be taken")
    blocked_reason: str | None = Field(description="Why the browser refused to show the app in a frame; None if not")


def take_screenshot(repo_root: Path, port: int) -> Screenshot:
    """A PNG of the page on ``port`` inside a frame, as a workspace window shows it, taken by the workspace's browser,
    and whether the browser refused to show it there."""
    python = repo_root / ".venv" / "bin" / "python"
    if not python.is_file():
        return Screenshot(image=None, blocked_reason=None)
    # A scratch folder of its own, so screenshots taken at the same moment never meet and nothing is left behind.
    with tempfile.TemporaryDirectory(prefix="app-store-screenshot-") as scratch:
        target = Path(scratch) / "screenshot.png"
        blocked = target.with_name(target.name + ".blocked")
        try:
            completed = subprocess.run(
                [str(python), "-I", str(SCREENSHOT_SCRIPT), f"http://127.0.0.1:{port}/", str(target)],
                capture_output=True,
                text=True,
                timeout=SCREENSHOT_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("No screenshot of port {}: {}", port, e)
            return Screenshot(image=None, blocked_reason=None)
        if completed.returncode != 0 or not target.is_file():
            logger.warning("No screenshot of port {}: {}", port, completed.stderr.strip()[-500:])
            return Screenshot(image=None, blocked_reason=None)
        reason = blocked.read_text(encoding="utf-8").strip() if blocked.is_file() else None
        return Screenshot(image=target.read_bytes(), blocked_reason=reason)


def read_help(program_path: Path, cwd: Path) -> str:
    """What ``program --help`` prints, trimmed; empty when it prints nothing or cannot run."""
    try:
        completed = subprocess.run(
            [str(program_path), "--help"],
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout + completed.stderr).strip()[:HELP_CHARACTERS]


def read_readme(registry: RegistryClientInterface, request: CheckedRequest, version: str) -> str:
    """The package's long description on its registry, trimmed; empty when there is none."""
    try:
        if request.source == "pypi":
            project = registry.get_json(f"{PYPI_ROOT}/pypi/{request.package}/{version}/json") or {}
            text = str((project.get("info") or {}).get("description") or "")
        else:
            packument = registry.get_json(f"{NPM_REGISTRY_ROOT}/{request.package}") or {}
            text = str(packument.get("readme") or "")
    except PackageLookupError:
        return ""
    return text.strip()[:README_CHARACTERS]


def curated_app_for(apps: tuple[CatalogApp, ...], source: str, package: str) -> CatalogApp | None:
    """The store's own app that installs ``package`` from ``source``, if it has one."""
    wanted = package.lower().replace("_", "-")
    for app in apps:
        recipe = app.recipe
        if source == "pypi" and isinstance(recipe, PythonRecipe):
            name = recipe.requirement.split("==", 1)[0]
        elif source == "npm" and isinstance(recipe, NodeRecipe):
            name = recipe.package.rsplit("@", 1)[0]
        else:
            continue
        if name.lower().replace("_", "-") == wanted:
            return app
    return None


class CommandFinder(MutableModel):
    """Runs the jobs that work out start commands, kept in memory and each logged to disk. Starting a job cancels the
    one before it, which stops at its next step."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    engine: InstallEngine = Field(frozen=True, description="Its installer, layout and catalog")
    checker: PackageChecker = Field(frozen=True, description="Pins and estimates the package")
    writer: CommandWriterInterface = Field(frozen=True, description="Writes the commands")
    trial_runner: TrialRunnerInterface = Field(frozen=True, description="Test-launches them")
    read_program_help: Callable[[Path, Path], str] = Field(default=read_help, frozen=True, description="--help")
    max_attempts: int = Field(default=MAX_ATTEMPTS, frozen=True, description="Tries before giving up")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _jobs: dict[str, FinderJob] = PrivateAttr(default_factory=dict)
    _screenshots: dict[str, bytes] = PrivateAttr(default_factory=dict)
    _cancels: dict[str, threading.Event] = PrivateAttr(default_factory=dict)

    @property
    def trials_dir(self) -> Path:
        return self.engine.layout.data_dir / TRIALS_DIRNAME

    def log_path(self, job_id: str) -> Path:
        return self.trials_dir / f"{job_id}.log"

    def start(self, request: AddAppRequest, is_threaded: bool = True) -> FinderJob:
        """Start working out ``request``'s command, cancelling any job still running; raises AddAppRefusedError when
        the package request itself breaks a rule."""
        catalog = self.engine.catalog
        curated = curated_app_for(self.engine.curated.apps, request.source, request.package.strip())
        if curated is not None:
            raise AddAppRefusedError(
                f"{curated.title} is already in the store, set up and ready to install in one click: close this and "
                "find it in the list."
            )
        taken = taken_app_names(
            {app.slug for app in catalog.apps},
            {str(row.name) for row in self.engine.read_registry_rows()},
            self.engine.layout.dropin_dir,
        )
        normalized = check_package_request(request, taken)
        job_id = uuid.uuid4().hex[:12]
        job = FinderJob(
            job_id=job_id,
            status=FinderStatus.WORKING,
            progress="Looking the package up",
            package=normalized.package,
            version="",
            command="",
            explanation="",
            attempts=(),
            has_screenshot=False,
            started_at=now_iso(),
        )
        cancel = threading.Event()
        with self._lock:
            for event in self._cancels.values():
                event.set()
            self._jobs[job_id] = job
            self._cancels[job_id] = cancel
            for old_id in list(self._jobs)[:-MAX_KEPT_JOBS]:
                if old_id not in self._cancels:
                    self._jobs.pop(old_id)
                    self._screenshots.pop(old_id, None)
        self.trials_dir.mkdir(parents=True, exist_ok=True)
        _append(self.log_path(job_id), f"[app-store] working out how to start {normalized.package} at {now_iso()}")
        self._prune_trials()
        if is_threaded:
            threading.Thread(
                target=self._run, args=(job_id, normalized, cancel), name=f"app-store-find-{job_id}", daemon=True
            ).start()
        else:
            self._run(job_id, normalized, cancel)
        return self.get(job_id) or job

    def get(self, job_id: str) -> FinderJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def screenshot(self, job_id: str) -> bytes | None:
        with self._lock:
            return self._screenshots.get(job_id)

    def cancel(self, job_id: str) -> FinderJob | None:
        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None:
            event.set()
        return self.get(job_id)

    def _prune_trials(self) -> None:
        """Delete the scratch folders and trial output no job of this run owns (left by a restart mid-job), and all
        but the newest logs."""
        try:
            entries = list(self.trials_dir.iterdir())
        except OSError as e:
            logger.warning("Could not read {}: {}", self.trials_dir, e)
            return
        # Read after the listing: a job is registered before its thread makes its folder, so every folder listed
        # belongs to a job read here, even one another request started meanwhile.
        with self._lock:
            known = set(self._jobs)
        for entry in entries:
            if entry.is_dir() and entry.name not in known:
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.suffix == TRIAL_OUTPUT_SUFFIX and entry.name.split(".", 1)[0] not in known:
                entry.unlink(missing_ok=True)
        logs = sorted((entry for entry in entries if entry.suffix == ".log"), key=_modified_at, reverse=True)
        for log in logs[MAX_KEPT_JOBS:]:
            log.unlink(missing_ok=True)

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            self._jobs[job_id] = self._jobs[job_id].model_copy(update=changes)

    def _finish(self, job_id: str, status: FinderStatus, explanation: str, **changes: object) -> None:
        self._update(job_id, status=status, progress="", explanation=explanation, **changes)
        _append(self.log_path(job_id), f"[app-store] finished: {status.value}: {explanation}")
        with self._lock:
            self._cancels.pop(job_id, None)

    def _run(self, job_id: str, request: CheckedRequest, cancel: threading.Event) -> None:
        trial_dir = self.trials_dir / job_id
        try:
            # A trial install is scratch: the backup keeps only the marker.
            trial_dir.mkdir(parents=True, exist_ok=True)
            (trial_dir / NO_BACKUP_MARKER).touch()
            self._work(job_id, request, cancel, trial_dir)
        except FinderError as e:
            self._finish(job_id, FinderStatus.FAILED, str(e))
        except (OSError, PortError) as e:
            logger.warning("Working out the command for {} failed: {}", request.package, e)
            self._finish(job_id, FinderStatus.FAILED, f"Something went wrong while trying the app out: {e}")
        finally:
            shutil.rmtree(trial_dir, ignore_errors=True)
            with self._lock:
                is_unfinished = job_id in self._cancels
            # An error nothing above expects still ends the thread, but the dialog polling the job is told.
            if is_unfinished:
                self._finish(job_id, FinderStatus.FAILED, "Something went wrong while trying the app out.")

    def _work(self, job_id: str, request: CheckedRequest, cancel: threading.Event, trial_dir: Path) -> None:
        log_path = self.log_path(job_id)
        registry = SOURCE_NAMES[request.source]
        try:
            facts = self.checker.pin(request)
        except AddAppRefusedError as e:
            raise FinderError(str(e)) from e
        except PackageLookupError as e:
            raise FinderError(f"{registry} couldn't be reached to look the package up. Try again in a moment.") from e
        self._update(job_id, version=facts.version)
        estimate = self.checker.estimate(request, facts.version)
        room = check_room(request.title, estimate.disk_mb, None, self.engine.resources.read_room())
        if room.verdict is RoomVerdict.WONT_FIT:
            raise FinderError(room.blocked_reason)

        if self._is_cancelled(job_id, cancel):
            return
        self._update(job_id, progress=f"Downloading {request.package} {facts.version} to try it out")
        env_dir, data_dir = trial_dir / "env", trial_dir / "data"
        try:
            self.engine.installer.install(recipe_for(request, facts.version), env_dir, log_path)
        except InstallError as e:
            raise FinderError(
                f"{request.package} {facts.version} couldn't be downloaded and installed, so it can't be tried out."
            ) from e
        bin_dir = env_dir / "bin" if request.source == "pypi" else env_dir / "node_modules" / ".bin"
        programs = installed_programs(bin_dir)
        if not programs:
            self._finish(
                job_id,
                FinderStatus.NOT_AN_APP,
                f"{request.package} doesn't include anything that can be started: it's a building block for other "
                "software, not an app with a window.",
            )
            return

        if self._is_cancelled(job_id, cancel):
            return
        self._update(job_id, progress="Reading its instructions")
        data_dir.mkdir(parents=True, exist_ok=True)
        help_texts = {
            program: self.read_program_help(bin_dir / program, data_dir) for program in programs[:MAX_PROGRAMS_READ]
        }
        context = PackageContext(
            registry=registry,
            package=request.package,
            version=facts.version,
            summary=facts.summary,
            programs=tuple(programs),
            help_texts=help_texts,
            readme=read_readme(self.checker.registry, request, facts.version),
        )

        failed: list[FailedTry] = []
        attempts: list[FinderAttempt] = []
        for number in range(1, self.max_attempts + 1):
            if self._is_cancelled(job_id, cancel):
                return
            self._update(
                job_id,
                progress="Figuring out how to start it"
                if number == 1
                else f"That didn't work, so trying another way ({number} of {self.max_attempts})",
            )
            try:
                proposal = self.writer.propose(context, failed, log_path)
            except UnreadableAnswerError:
                failed.append(
                    FailedTry(
                        command="(none)",
                        what_happened="Your answer could not be read. Answer with only the JSON object described.",
                    )
                )
                attempts.append(FinderAttempt(command="", outcome="The AI's answer couldn't be read."))
                self._update(job_id, attempts=tuple(attempts))
                continue
            if proposal.kind == "not_an_app":
                self._finish(job_id, FinderStatus.NOT_AN_APP, proposal.explanation, attempts=tuple(attempts))
                return
            if proposal.kind == "needs_setup":
                self._finish(job_id, FinderStatus.NEEDS_SETUP, proposal.explanation, attempts=tuple(attempts))
                return
            command = proposal.command.strip()
            refusal = self._refusal(command, programs)
            setup_files: tuple[SetupFile, ...] = ()
            if refusal is None:
                try:
                    setup_files = check_setup_files(proposal.files)
                except AddAppRefusedError as e:
                    refusal = str(e)
            if refusal is not None:
                failed.append(FailedTry(command=command, what_happened=f"The app store refused it: {refusal}"))
                attempts.append(FinderAttempt(command=command, outcome="It broke one of the store's rules."))
                _append(log_path, f"[app-store] refused {command!r}: {refusal}")
                self._update(job_id, attempts=tuple(attempts))
                continue
            if self._is_cancelled(job_id, cancel):
                return
            program, arguments = check_command(command)
            program_path = bin_dir / program
            port = self._free_port()
            # Each try starts from an empty folder holding only its own settings files.
            shutil.rmtree(data_dir, ignore_errors=True)
            data_dir.mkdir(parents=True)
            for setup_file in setup_files:
                target = data_dir / setup_file.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_setup_content(setup_file.content, port, data_dir), encoding="utf-8")
                _append(log_path, f"[app-store] wrote {setup_file.path}:\n{setup_file.content}")
            self._update(job_id, progress="Test-launching it")
            outcome = self.trial_runner.run(
                render_argv((str(program_path), *arguments), port, data_dir, env_dir), data_dir, port, log_path, cancel
            )
            if outcome.is_answering:
                attempts.append(FinderAttempt(command=command, outcome="It started and showed a page."))
                if outcome.screenshot is not None:
                    with self._lock:
                        self._screenshots[job_id] = outcome.screenshot
                self._finish(
                    job_id,
                    FinderStatus.FOUND,
                    proposal.explanation,
                    command=command,
                    setup_files=setup_files,
                    attempts=tuple(attempts),
                    has_screenshot=outcome.screenshot is not None,
                )
                return
            if self._is_cancelled(job_id, cancel):
                return
            attempts.append(FinderAttempt(command=command, outcome=outcome.what_happened))
            failed.append(
                FailedTry(
                    command=command,
                    what_happened=f"{outcome.what_happened}\nIts output:\n{outcome.output_tail or '(nothing)'}"
                    + "".join(f"\nIt was started with {file.path}:\n{file.content}" for file in setup_files),
                )
            )
            self._update(job_id, attempts=tuple(attempts))
        last = attempts[-1].outcome if attempts else ""
        self._finish(
            job_id,
            FinderStatus.FAILED,
            f"Couldn't get {request.title} to start after {self.max_attempts} tries. The last time: {last} You can "
            "still type the start command yourself.",
            attempts=tuple(attempts),
        )

    def _refusal(self, command: str, programs: list[str]) -> str | None:
        try:
            program, _ = check_command(command)
        except AddAppRefusedError as e:
            return str(e)
        if program not in programs:
            return f"{program!r} isn't one of the programs it installed ({', '.join(programs)})."
        return None

    def _free_port(self) -> int:
        layout = self.engine.layout
        held = [record.port for record in self.engine.records.read().records if record.port is not None]
        return pick_free_port(layout.supervisord_conf, layout.registry_path, held, self.engine.is_bound)

    def _is_cancelled(self, job_id: str, cancel: threading.Event) -> bool:
        if not cancel.is_set():
            return False
        self._finish(job_id, FinderStatus.CANCELLED, "Stopped.")
        return True


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the process and everything it started. The whole group is signalled even when the process itself has
    already exited, since a launcher that starts its server and exits leaves the server running in the group; what
    is left once the process has stopped, or its grace period has run out, is killed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=TRIAL_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            continue


def _modified_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-OUTPUT_TAIL_LINES:])


def _append(log_path: Path, text: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(text.rstrip("\n") + "\n")
    except OSError as e:
        logger.warning("Could not write to {}: {}", log_path, e)
