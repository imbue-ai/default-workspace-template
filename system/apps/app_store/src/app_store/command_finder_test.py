"""Working out an added app's start command: each ending the person can see (found, not an app, needs setup, gave
up, refused), the retries that carry what went wrong back to the AI, the scratch folder always deleted, and the real
test-launch of a process."""

import json
import os
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest
from flask import Flask

from app_store import command_finder
from app_store.added_apps import AddAppRequest
from app_store.command_finder import ClaudeCommandWriter
from app_store.command_finder import CommandFinder
from app_store.command_finder import FailedTry
from app_store.command_finder import FinderError
from app_store.command_finder import FinderJob
from app_store.command_finder import FinderStatus
from app_store.command_finder import MAX_KEPT_JOBS
from app_store.command_finder import PackageContext
from app_store.command_finder import ProcessTrialRunner
from app_store.command_finder import Proposal
from app_store.command_finder import Screenshot
from app_store.command_finder import TrialOutcome
from app_store.command_finder import UnreadableAnswerError
from app_store.command_finder import framing_refusal
from app_store.command_finder import parse_proposal
from app_store.command_finder import read_help
from app_store.command_finder import render_prompt
from app_store.engine import InstallEngine
from app_store.errors import AddAppRefusedError
from app_store.layout import NO_BACKUP_MARKER
from app_store.pages import build_pages_blueprint
from app_store.ports import is_port_bound
from app_store.system import AnyRecipe
from app_store.template_sizes import TemplateSizeStore
from app_store.testing import FakeCommandWriter
from app_store.testing import FakePackageInstaller
from app_store.testing import FakeRepositorySizeFetcher
from app_store.testing import FakeTrialRunner
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import build_test_finder
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher

_GOOD = "demo serve --port {port} --host 127.0.0.1"
_ANSWERED = TrialOutcome(is_answering=True, what_happened="", output_tail="Serving", screenshot=b"\x89PNG")
_EXITED = TrialOutcome(
    is_answering=False, what_happened="It stopped right away (exit status 2).", output_tail="bad flag"
)


def _request(**overrides: str) -> AddAppRequest:
    return AddAppRequest.model_validate({"source": "pypi", "package": "demo", "display_name": "Demo", **overrides})


def _web_app(command: str = _GOOD) -> Proposal:
    return Proposal(kind="web_app", command=command, explanation="A demo you open in your browser.")


def _run(finder: CommandFinder, request: AddAppRequest | None = None) -> str:
    return finder.start(request or _request(), is_threaded=False).job_id


def _writer(finder: CommandFinder) -> FakeCommandWriter:
    assert isinstance(finder.writer, FakeCommandWriter)
    return finder.writer


def _runner(finder: CommandFinder) -> FakeTrialRunner:
    assert isinstance(finder.trial_runner, FakeTrialRunner)
    return finder.trial_runner


def test_a_command_that_answers_is_found_with_its_screenshot_and_the_scratch_folder_is_deleted(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    finder = build_test_finder(engine, tmp_path, proposals=[_web_app()], outcomes=[_ANSWERED])
    job_id = _run(finder)

    job = finder.get(job_id)
    assert job is not None
    assert job.status is FinderStatus.FOUND
    assert job.command == _GOOD
    assert job.version == "1.1"
    assert job.explanation == "A demo you open in your browser."
    assert job.has_screenshot and finder.screenshot(job_id) == b"\x89PNG"
    assert [attempt.outcome for attempt in job.attempts] == ["It started and showed a page."]
    # The AI was shown the program the package installed and its own --help.
    context = _writer(finder).contexts[0]
    assert context.programs == ("demo",)
    assert "usage: demo" in context.help_texts["demo"]
    # It ran from the scratch environment, with the placeholders filled in.
    argv = _runner(finder).launched[0]
    assert argv[0].endswith(f".command-trials/{job_id}/env/bin/demo")
    assert "{port}" not in " ".join(argv) and "127.0.0.1" in argv
    assert not (finder.trials_dir / job_id).exists()
    assert "finished: FOUND" in finder.log_path(job_id).read_text()
    # Nothing was added to the store or installed for real.
    assert engine.added.read() == ()
    assert engine.records.read().records == ()


def test_a_failed_launch_goes_back_to_the_ai_with_its_output_and_the_next_command_is_tried(tmp_path: Path) -> None:
    proposals = [_web_app("demo --bad {port} 127.0.0.1"), _web_app()]
    finder = build_test_finder(
        build_test_engine(tmp_path), tmp_path, proposals=proposals, outcomes=[_EXITED, _ANSWERED]
    )
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FOUND
    assert job.command == _GOOD
    assert [attempt.outcome for attempt in job.attempts] == [
        "It stopped right away (exit status 2).",
        "It started and showed a page.",
    ]
    second_try_saw = _writer(finder).failures_seen[1]
    assert second_try_saw[0].command == "demo --bad {port} 127.0.0.1"
    assert "bad flag" in second_try_saw[0].what_happened


def test_a_command_breaking_the_rules_is_never_launched_and_the_ai_is_told_why(tmp_path: Path) -> None:
    proposals = [_web_app("demo serve --port 8000"), _web_app("other --port {port} --host 127.0.0.1"), _web_app()]
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path, proposals=proposals, outcomes=[_ANSWERED])
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FOUND
    assert len(_runner(finder).launched) == 1
    seen = _writer(finder).failures_seen[2]
    assert "{port}" in seen[0].what_happened
    assert "isn't one of the programs it installed (demo)" in seen[1].what_happened


def test_it_gives_up_after_the_last_try_and_says_so_plainly(tmp_path: Path) -> None:
    proposals = [_web_app(), _web_app(), _web_app()]
    finder = build_test_finder(
        build_test_engine(tmp_path), tmp_path, proposals=proposals, outcomes=[_EXITED, _EXITED, _EXITED]
    )
    job_id = _run(finder)
    job = finder.get(job_id)

    assert job is not None and job.status is FinderStatus.FAILED
    assert job.explanation.startswith("Couldn't get Demo to start after 3 tries.")
    assert "type the start command yourself" in job.explanation
    assert len(job.attempts) == 3
    assert not (finder.trials_dir / job_id).exists()


@pytest.mark.parametrize(
    ("kind", "status"), [("not_an_app", FinderStatus.NOT_AN_APP), ("needs_setup", FinderStatus.NEEDS_SETUP)]
)
def test_the_ais_verdict_that_it_is_not_an_app_or_needs_setup_ends_without_a_launch(
    tmp_path: Path, kind: str, status: FinderStatus
) -> None:
    proposal = Proposal.model_validate({"kind": kind, "explanation": "It needs an API key from OpenAI first."})
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path, proposals=[proposal])
    job = finder.get(_run(finder))

    assert job is not None and job.status is status
    assert job.explanation == "It needs an API key from OpenAI first."
    assert _runner(finder).launched == []


class _LibraryInstaller(FakePackageInstaller):
    """Installs a package that provides no programs at all."""

    def install(self, recipe: AnyRecipe, env_dir: Path, log_path: Path) -> None:
        (env_dir / "bin").mkdir(parents=True)
        (env_dir / "bin" / "python").write_text("")


def test_a_package_with_no_programs_is_not_an_app_and_the_ai_is_never_asked(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path).model_copy(update={"installer": _LibraryInstaller()})
    finder = build_test_finder(engine, tmp_path)
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.NOT_AN_APP
    assert "not an app with a window" in job.explanation
    assert _writer(finder).contexts == []


def test_an_install_that_fails_ends_with_a_plain_reason(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path).model_copy(update={"installer": FakePackageInstaller(is_failing=True)})
    finder = build_test_finder(engine, tmp_path)
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FAILED
    assert job.explanation == "demo 1.1 couldn't be downloaded and installed, so it can't be tried out."


def test_a_package_the_registry_does_not_have_ends_with_its_refusal(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path)
    job = finder.get(_run(finder, _request(package="nosuchthing", display_name="")))

    assert job is not None and job.status is FinderStatus.FAILED
    assert job.explanation == "There's no package called 'nosuchthing' on PyPI."


def test_an_unreadable_answer_is_asked_again(tmp_path: Path) -> None:
    class _OnceUnreadable(FakeCommandWriter):
        def propose(self, context, failed, log_path):  # noqa: ANN001, ANN202
            if not self.failures_seen:
                self.failures_seen.append(list(failed))
                raise UnreadableAnswerError("The AI's answer couldn't be read.")
            return super().propose(context, failed, log_path)

    engine = build_test_engine(tmp_path)
    finder = build_test_finder(engine, tmp_path, outcomes=[_ANSWERED]).model_copy(
        update={"writer": _OnceUnreadable(proposals=[_web_app(), _web_app()])}
    )
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FOUND
    assert job.attempts[0].outcome == "The AI's answer couldn't be read."


_UNREACHABLE = "The AI that works out start commands couldn't be reached. Try again in a moment."


class _UnreachableWriter(FakeCommandWriter):
    """Fails every try the way the production writer does when ``claude -p`` cannot be run."""

    def propose(self, context: PackageContext, failed: list[FailedTry], log_path: Path) -> Proposal:
        self.contexts.append(context)
        raise FinderError(_UNREACHABLE)


def test_an_ai_that_cannot_be_reached_ends_the_job_without_another_try(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path).model_copy(
        update={"writer": _UnreachableWriter()}
    )
    job_id = _run(finder)
    job = finder.get(job_id)

    assert job is not None and job.status is FinderStatus.FAILED
    assert job.explanation == _UNREACHABLE
    assert len(_writer(finder).contexts) == 1
    assert _runner(finder).launched == []
    assert not (finder.trials_dir / job_id).exists()


class _BrokenWriter(FakeCommandWriter):
    """Fails with an error nothing in the job expects."""

    def propose(self, context: PackageContext, failed: list[FailedTry], log_path: Path) -> Proposal:
        raise ValueError("an unexpected bug")


def test_an_unexpected_error_still_ends_the_job_so_the_dialog_is_not_left_waiting(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path).model_copy(update={"writer": _BrokenWriter()})
    with pytest.raises(ValueError, match="an unexpected bug"):
        finder.start(_request(), is_threaded=False)
    (job_id,) = [path.stem for path in finder.trials_dir.glob("*.log")]
    job = finder.get(job_id)

    assert job is not None and job.status is FinderStatus.FAILED
    assert job.explanation == "Something went wrong while trying the app out."
    assert job.progress == ""
    assert not (finder.trials_dir / job_id).exists()


def test_a_package_the_store_already_offers_is_pointed_at_instead(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path)
    with pytest.raises(AddAppRefusedError, match="TiddlyWiki is already in the store"):
        finder.start(AddAppRequest(source="npm", package="tiddlywiki"), is_threaded=False)
    with pytest.raises(AddAppRefusedError, match="Datasette is already in the store"):
        finder.start(AddAppRequest(source="pypi", package="Datasette"), is_threaded=False)


def test_starting_a_new_job_cancels_the_one_still_running(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path)
    first = finder.start(_request(), is_threaded=False)
    second_event = threading.Event()
    with finder._lock:
        finder._cancels[first.job_id] = second_event
    finder.start(_request(package="nosuchthing", display_name=""), is_threaded=False)
    assert second_event.is_set()


def test_a_job_cancelled_mid_way_stops_before_launching_and_cleans_up(tmp_path: Path) -> None:
    holder: dict[str, CommandFinder] = {}

    class _CancellingWriter(FakeCommandWriter):
        def propose(self, context, failed, log_path):  # noqa: ANN001, ANN202
            job_id = log_path.stem
            holder["finder"].cancel(job_id)
            return super().propose(context, failed, log_path)

    finder = build_test_finder(build_test_engine(tmp_path), tmp_path, outcomes=[_ANSWERED]).model_copy(
        update={"writer": _CancellingWriter(proposals=[_web_app()])}
    )
    holder["finder"] = finder
    job_id = _run(finder)
    job = finder.get(job_id)

    assert job is not None and job.status is FinderStatus.CANCELLED
    assert _runner(finder).launched == []
    assert not (finder.trials_dir / job_id).exists()
    assert finder.cancel("missing") is None


def test_the_answer_is_read_from_around_the_json_and_extra_keys_are_ignored() -> None:
    proposal = parse_proposal('Sure:\n{"kind": "needs_setup", "command": "", "explanation": "Key.", "note": ""}\n')
    assert proposal.kind == "needs_setup"
    with pytest.raises(FinderError):
        parse_proposal("no json here")
    with pytest.raises(FinderError):
        parse_proposal('{"kind": "maybe", "explanation": ""}')


def test_the_prompt_carries_the_package_its_programs_help_readme_and_failed_tries(tmp_path: Path) -> None:
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path, proposals=[_web_app()], outcomes=[_EXITED])
    _run(finder)
    context = _writer(finder).contexts[0]
    prompt = render_prompt(context, _writer(finder).failures_seen[0])
    assert "Package: demo 1.1, from PyPI." in prompt
    assert "Programs it installed: demo" in prompt
    assert "--- demo --help ---" in prompt


# Starts a server, waits until it is listening (so the trial has a real server to stop), then exits.
_LAUNCHER_THAT_LEAVES_ITS_SERVER = """
import socket, subprocess, sys, threading
subprocess.Popen([sys.executable, "-m", "http.server", sys.argv[1], "--bind", "127.0.0.1"])
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.1).close()
        break
    except OSError:
        threading.Event().wait(0.05)
"""


def test_a_server_left_running_by_a_launcher_that_exits_is_stopped_too(tmp_path: Path) -> None:
    runner = ProcessTrialRunner(repo_root=tmp_path, start_timeout_seconds=15)
    port = next(port for port in range(18600, 18700) if not is_port_bound(port))
    outcome = runner.run(
        [sys.executable, "-c", _LAUNCHER_THAT_LEAVES_ITS_SERVER, str(port)],
        tmp_path / "served",
        port,
        tmp_path / "trial.log",
        threading.Event(),
    )
    # Whether the trial saw the server answer or the launcher exit first is a race; either way the trial is over.
    assert outcome.is_answering or outcome.what_happened == "It stopped right away (exit status 0)."
    # The server the launcher started is gone with it: nothing listens on its port any more. (A bind test would be
    # fooled by the closed connections the trial's own checks leave waiting on the port.)
    assert _stops_listening_soon(port)


def _stops_listening_soon(port: int) -> bool:
    """Whether connecting to ``port`` is refused, now or within a couple of seconds."""
    pause = threading.Event()
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        except OSError:
            return True
        pause.wait(0.05)
    return False


def test_a_real_launch_that_answers_and_one_that_exits(tmp_path: Path) -> None:
    runner = ProcessTrialRunner(repo_root=tmp_path, start_timeout_seconds=15)
    port = next(port for port in range(18700, 18800) if not is_port_bound(port))
    log_path = tmp_path / "trial.log"
    answered = runner.run(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        tmp_path / "served",
        port,
        log_path,
        threading.Event(),
    )
    assert answered.is_answering
    # No browser in the test's workspace: the screenshot is simply missing.
    assert answered.screenshot is None
    # It was stopped: nothing answers there any more.
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/", timeout=2)

    exited = runner.run(
        [sys.executable, "-c", "print('boom'); raise SystemExit(3)"], tmp_path, port, log_path, threading.Event()
    )
    assert not exited.is_answering
    assert exited.what_happened == "It stopped right away (exit status 3)."
    assert "boom" in exited.output_tail
    assert "boom" in log_path.read_text()


def test_the_routes_start_poll_and_serve_the_screenshot_and_log(tmp_path: Path) -> None:
    engine: InstallEngine = build_test_engine(tmp_path)
    finder = build_test_finder(
        engine, tmp_path, proposals=[_web_app()], outcomes=[_ANSWERED], finder_type=_SynchronousFinder
    )
    app = Flask("app-store-under-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            tmp_path / "static",
            engine,
            build_test_adder(engine, tmp_path),
            finder,
            TemplateCatalogStore(catalog_url="", cache_path=tmp_path / "c.json", fetcher=FakeTemplateCatalogFetcher()),
            TemplateSizeStore(cache_path=tmp_path / "s.json", fetcher=FakeRepositorySizeFetcher()),
            tmp_path / "contract.js",
        )
    )
    client = app.test_client()

    started = client.post("/api/added-apps/find-command", json={"source": "pypi", "package": "demo"})
    assert started.status_code == 202
    job_id = started.get_json()["job"]["job_id"]
    polled = client.get(f"/api/added-apps/find-command/{job_id}").get_json()["job"]
    assert polled["status"] == "FOUND" and polled["command"] == _GOOD
    screenshot = client.get(f"/api/added-apps/find-command/{job_id}/screenshot.png")
    assert (screenshot.data, screenshot.mimetype) == (b"\x89PNG", "image/png")
    assert screenshot.headers["X-Content-Type-Options"] == "nosniff"
    log = client.get(f"/api/added-apps/find-command/{job_id}/log")
    assert "finished: FOUND" in log.get_data(as_text=True)
    assert (log.mimetype, log.headers["X-Content-Type-Options"]) == ("text/plain", "nosniff")
    assert client.post(f"/api/added-apps/find-command/{job_id}/cancel").status_code == 200

    assert client.get("/api/added-apps/find-command/nope").status_code == 404
    assert client.post("/api/added-apps/find-command/nope/cancel").status_code == 404
    assert client.get("/api/added-apps/find-command/nope/screenshot.png").status_code == 404
    assert client.get("/api/added-apps/find-command/nope/log").status_code == 404
    refused = client.post("/api/added-apps/find-command", json={"source": "npm", "package": "tiddlywiki"})
    assert refused.status_code == 422


class _SynchronousFinder(CommandFinder):
    """Runs each job before answering, so a route test sees its ending."""

    def start(self, request: AddAppRequest, is_threaded: bool = True) -> FinderJob:
        return super().start(request, is_threaded=False)


@pytest.mark.parametrize(
    ("headers", "refusal"),
    [
        ({}, None),
        ({"Content-Security-Policy": "default-src 'self'; frame-ancestors *"}, None),
        ({"X-Frame-Options": "SAMEORIGIN"}, "X-Frame-Options: SAMEORIGIN"),
        (
            {"Content-Security-Policy": "frame-ancestors 'self'; report-uri /x"},
            "Content-Security-Policy: frame-ancestors 'self'",
        ),
    ],
)
def test_a_page_that_forbids_being_framed_is_caught(headers: dict[str, str], refusal: str | None) -> None:
    assert framing_refusal(httpx.Headers(headers)) == refusal


_FRAME_REFUSING_SERVER = """
import http.server, sys
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        super().end_headers()
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def test_a_real_launch_whose_page_refuses_to_be_framed_fails_and_tells_the_ai_why(tmp_path: Path) -> None:
    runner = ProcessTrialRunner(repo_root=tmp_path, start_timeout_seconds=15)
    port = next(port for port in range(18800, 18900) if not is_port_bound(port))
    outcome = runner.run(
        [sys.executable, "-c", _FRAME_REFUSING_SERVER, str(port)],
        tmp_path,
        port,
        tmp_path / "trial.log",
        threading.Event(),
    )
    assert not outcome.is_answering
    assert (
        outcome.what_happened
        == "It started, but it refuses to be shown inside a window, so its window would be blank."
    )
    assert "frame-ancestors 'self'" in outcome.output_tail
    assert outcome.screenshot is None


class _MarkerSeeingRunner(FakeTrialRunner):
    """Also notes whether the scratch folder it runs in is marked for the backup to skip."""

    saw_marker: list[bool] = []

    def run(self, argv: list[str], cwd: Path, port: int, log_path: Path, cancelled: threading.Event) -> TrialOutcome:
        self.saw_marker.append((cwd.parent / NO_BACKUP_MARKER).is_file())
        return super().run(argv, cwd, port, log_path, cancelled)


def test_a_trial_install_is_marked_for_the_backup_to_skip(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    runner = _MarkerSeeingRunner(outcomes=[_ANSWERED], saw_marker=[])
    finder = build_test_finder(engine, tmp_path, proposals=[_web_app()])
    finder = finder.model_copy(update={"trial_runner": runner})
    _run(finder)
    assert runner.saw_marker == [True]


def test_scratch_left_by_a_restart_and_all_but_the_newest_logs_are_deleted(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    finder = build_test_finder(engine, tmp_path, proposals=[_web_app()], outcomes=[_ANSWERED])
    leftover = finder.trials_dir / "0123456789ab" / "env" / "bin"
    leftover.mkdir(parents=True)
    leftover_output = finder.trials_dir / "0123456789ab.try-8123.out"
    leftover_output.write_text("a test launch's output")
    old_logs = []
    for number in range(MAX_KEPT_JOBS + 5):
        log = finder.trials_dir / f"old{number:02d}.log"
        log.write_text("an earlier job")
        os.utime(log, (1_000_000 + number, 1_000_000 + number))
        old_logs.append(log)

    job_id = _run(finder)

    assert not (finder.trials_dir / "0123456789ab").exists()
    assert not leftover_output.exists()
    logs = sorted(path.name for path in finder.trials_dir.glob("*.log"))
    assert len(logs) == MAX_KEPT_JOBS
    # The newest are kept: this job's log and the most recent earlier ones.
    assert f"{job_id}.log" in logs
    assert old_logs[-1].name in logs and old_logs[0].name not in logs


def test_only_the_newest_jobs_are_kept_in_memory(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    count = MAX_KEPT_JOBS + 2
    finder = build_test_finder(engine, tmp_path, proposals=[_web_app()] * count, outcomes=[_ANSWERED] * count)
    writer = _writer(finder)
    job_ids = []
    for _ in range(count):
        # Each job asks once; the fake writer counts tries across jobs, so give each its own first answer.
        writer.failures_seen.clear()
        job_ids.append(_run(finder))
    assert finder.get(job_ids[0]) is None and finder.screenshot(job_ids[0]) is None
    assert finder.get(job_ids[1]) is None
    assert finder.get(job_ids[-MAX_KEPT_JOBS]) is not None
    last = finder.get(job_ids[-1])
    assert last is not None and last.status is FinderStatus.FOUND
    assert finder.screenshot(job_ids[-1]) == b"\x89PNG"


def _stub_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


_CONTEXT = PackageContext(
    registry="PyPI", package="demo", version="1.1", summary="", programs=("demo",), help_texts={}, readme=""
)


def test_the_production_writer_reads_the_ais_answer_and_logs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = json.dumps({"kind": "web_app", "command": _GOOD, "explanation": "A demo."})
    payload = json.dumps({"subtype": "success", "result": answer, "total_cost_usd": 0.002})
    (tmp_path / "answer.json").write_text(payload)
    _stub_claude(tmp_path, monkeypatch, f"cat '{tmp_path / 'answer.json'}'\n")
    log = tmp_path / "job.log"
    proposal = ClaudeCommandWriter(timeout_seconds=30).propose(_CONTEXT, [], log)
    assert (proposal.kind, proposal.command) == ("web_app", _GOOD)
    assert "the AI answered ($0.0020)" in log.read_text()


@pytest.mark.parametrize("body", ["sleep 5\n", "exit 3\n"], ids=["hangs", "fails"])
def test_an_ai_that_hangs_or_fails_ends_in_a_plain_reason_not_a_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _stub_claude(tmp_path, monkeypatch, body)
    log = tmp_path / "job.log"
    with pytest.raises(FinderError, match="couldn't be reached"):
        ClaudeCommandWriter(timeout_seconds=0.5).propose(_CONTEXT, [], log)
    assert "the AI could not be reached" in log.read_text()


def test_a_programs_help_is_read_and_trimmed_and_a_broken_one_reads_as_nothing(tmp_path: Path) -> None:
    program = tmp_path / "demo"
    program.write_text("#!/bin/sh\necho usage: demo --port PORT\necho oops >&2\n")
    program.chmod(0o755)
    assert read_help(program, tmp_path) == "usage: demo --port PORT\noops"
    assert read_help(tmp_path / "missing", tmp_path) == ""


def test_a_page_the_browser_will_not_show_in_a_frame_fails_even_when_its_headers_look_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        command_finder,
        "take_screenshot",
        lambda repo_root, port: Screenshot(image=b"\x89PNG", blocked_reason="Framing 'x' violates frame-ancestors"),
    )
    runner = ProcessTrialRunner(repo_root=tmp_path, start_timeout_seconds=15)
    port = next(port for port in range(18900, 19000) if not is_port_bound(port))
    outcome = runner.run(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        tmp_path / "served",
        port,
        tmp_path / "trial.log",
        threading.Event(),
    )
    assert not outcome.is_answering
    assert (
        outcome.what_happened
        == "It started, but it refuses to be shown inside a window, so its window would be blank."
    )
    assert "violates frame-ancestors" in outcome.output_tail


def test_settings_files_the_ai_writes_are_in_the_trial_folder_and_come_back_with_the_command(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class _ReadingRunner(FakeTrialRunner):
        def run(self, argv, cwd, port, log_path, cancelled):  # noqa: ANN001, ANN202
            seen["config"] = (cwd / "config.yaml").read_text()
            seen["port"] = str(port)
            return super().run(argv, cwd, port, log_path, cancelled)

    proposal = Proposal.model_validate(
        {
            "kind": "web_app",
            "command": "demo --config {data_dir}/config.yaml --port {port} --host 127.0.0.1",
            "files": [{"path": "config.yaml", "content": "listen: {port}\nheaders: {x: 1}\n"}],
            "explanation": "A demo.",
        }
    )
    finder = build_test_finder(build_test_engine(tmp_path), tmp_path, proposals=[proposal]).model_copy(
        update={"trial_runner": _ReadingRunner(outcomes=[_ANSWERED])}
    )
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FOUND
    assert seen["config"] == f"listen: {seen['port']}\nheaders: {{x: 1}}\n"
    assert [(file.path, file.content) for file in job.setup_files] == [
        ("config.yaml", "listen: {port}\nheaders: {x: 1}\n")
    ]


def test_a_code_file_from_the_ai_is_refused_and_the_ai_is_told_why(tmp_path: Path) -> None:
    with_code = Proposal.model_validate(
        {
            "kind": "web_app",
            "command": _GOOD,
            "files": [{"path": "plugins/allow/index.js", "content": "module.exports = {}"}],
            "explanation": "A demo.",
        }
    )
    finder = build_test_finder(
        build_test_engine(tmp_path), tmp_path, proposals=[with_code, _web_app()], outcomes=[_ANSWERED]
    )
    job = finder.get(_run(finder))

    assert job is not None and job.status is FinderStatus.FOUND and job.setup_files == ()
    assert len(_runner(finder).launched) == 1
    assert "never code" in _writer(finder).failures_seen[1][0].what_happened
