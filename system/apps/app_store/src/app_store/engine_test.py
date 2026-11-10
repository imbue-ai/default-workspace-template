"""The install engine with every reach into the system faked: install, failure and undo, refusals, uninstall, the
queue, and the startup reconcile."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import Field

from app_manifest.manifest import load_manifest
from app_store.engine import INTERRUPTED_REASON
from app_store.engine import InstallEngine
from app_store.errors import InstallRefusedError
from app_store.layout import INITIALIZED_MARKER
from app_store.layout import NO_BACKUP_MARKER
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import RecordStore
from app_store.records import now_iso
from app_store.room import WorkspaceRoom
from app_store.testing import FakeCommandRunner
from app_store.testing import FakePackageInstaller
from app_store.testing import FakePortProbe
from app_store.testing import FakeProcessMemoryReader
from app_store.testing import FakeRegistrar
from app_store.testing import FakeResourceReader
from app_store.testing import FakeSupervisor
from app_store.testing import build_test_engine
from app_store.testing import make_registry_row


def _fakes(
    engine: InstallEngine,
) -> tuple[FakePackageInstaller, FakeCommandRunner, FakeSupervisor, FakeRegistrar, FakePortProbe]:
    assert isinstance(engine.installer, FakePackageInstaller)
    assert isinstance(engine.runner, FakeCommandRunner)
    assert isinstance(engine.supervisor, FakeSupervisor)
    assert isinstance(engine.registrar, FakeRegistrar)
    assert isinstance(engine.probe, FakePortProbe)
    return engine.installer, engine.runner, engine.supervisor, engine.registrar, engine.probe


def _status(engine: InstallEngine, slug: str) -> InstallRecord:
    record = engine.records.read().find(slug)
    assert record is not None
    return record


def _install(engine: InstallEngine, slug: str) -> InstallRecord:
    engine.request_install(slug)
    assert engine.run_next_job()
    return _status(engine, slug)


def test_an_install_makes_the_environment_files_and_program_then_records_the_measured_disk(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    installer, _runner, supervisor, _registrar, probe = _fakes(engine)

    queued = engine.request_install("datasette")
    assert (queued.status, queued.job) == (InstallStatus.QUEUED, JobKind.INSTALL)
    assert engine.records.read().queue == ("datasette",)
    assert engine.run_next_job()

    record = _status(engine, "datasette")
    assert (record.status, record.job, record.failure_reason) == (InstallStatus.INSTALLED, None, "")
    assert record.port == 8080
    assert record.measured_disk_mb is not None and record.measured_disk_mb >= 3
    assert installer.installed == ["datasette==0.65.5"]
    layout = engine.layout
    assert (layout.env_dir("datasette") / NO_BACKUP_MARKER).is_file()
    assert layout.app_data_dir("datasette").is_dir()
    manifest = load_manifest(layout.manifest_path("datasette"))
    assert str(manifest.name) == "datasette"
    assert layout.icon_path("datasette").read_text().startswith("<svg")
    assert "app-store-launch datasette" in layout.dropin_path("datasette").read_text()
    assert supervisor.programs == {"datasette"}
    assert probe.probed == [8080]
    assert "installed; it takes" in layout.log_path("datasette").read_text()
    assert engine.records.read().queue == ()
    assert not engine.run_next_job()


def test_a_failed_package_install_undoes_everything_but_the_log_and_says_why(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    installer, _runner, supervisor, registrar, _probe = _fakes(engine)
    installer.is_failing = True

    record = _install(engine, "marimo")

    assert (record.status, record.job) == (InstallStatus.FAILED, None)
    assert record.failure_reason == "Installing marimo==0.25.2 failed (see the install log)."
    assert not engine.layout.env_dir("marimo").exists()
    assert not engine.layout.dropin_path("marimo").exists()
    assert supervisor.programs == set()
    assert registrar.unregistered == ["marimo"]
    assert "failed: Installing marimo" in engine.layout.log_path("marimo").read_text()


def test_an_app_that_never_answers_is_removed_again_and_marked_failed(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, _registrar, probe = _fakes(engine)
    probe.is_answering = False

    record = _install(engine, "radicale")

    assert record.status is InstallStatus.FAILED
    assert "didn't start answering" in record.failure_reason
    assert "Its own output is at the end of the install log" in record.failure_reason
    assert supervisor.calls == ["apply radicale", "stop radicale", "apply radicale"]
    assert supervisor.programs == set()
    assert not engine.layout.manifest_path("radicale").exists()


def test_a_failed_install_can_be_tried_again(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    installer, *_ = _fakes(engine)
    installer.is_failing = True
    _install(engine, "datasette")
    installer.is_failing = False

    assert _install(engine, "datasette").status is InstallStatus.INSTALLED


def test_an_apps_init_runs_once_before_its_first_start(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, runner, *_ = _fakes(engine)

    _install(engine, "tiddlywiki")
    data_dir = engine.layout.app_data_dir("tiddlywiki")
    env_dir = engine.layout.env_dir("tiddlywiki")
    assert runner.commands == [[f"{env_dir}/node_modules/.bin/tiddlywiki", f"{data_dir}/wiki", "--init", "server"]]
    assert runner.cwds == [data_dir], "it runs in the app's data folder, as the app itself and a trial do"
    assert (data_dir / INITIALIZED_MARKER).is_file()

    engine.request_uninstall("tiddlywiki", is_deleting_data=False)
    engine.run_next_job()
    _install(engine, "tiddlywiki")
    assert len(runner.commands) == 1, "a reinstall over kept data does not re-initialise it"


def test_a_failing_init_fails_the_install(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, runner, *_ = _fakes(engine)
    runner.exit_status = 1

    record = _install(engine, "tiddlywiki")

    assert record.status is InstallStatus.FAILED
    assert record.failure_reason == "Setting up TiddlyWiki's data folder failed (see the install log)."


def test_a_refused_program_change_fails_the_install(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, *_ = _fakes(engine)
    supervisor.is_refusing = True

    record = _install(engine, "datasette")

    assert record.status is InstallStatus.FAILED
    assert "supervisord refused" in record.failure_reason


def test_installs_run_one_at_a_time_in_the_order_asked(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    engine.request_install("radicale")
    engine.request_install("datasette")

    assert engine.records.read().queue == ("radicale", "datasette")
    assert engine.run_next_job()
    assert _status(engine, "radicale").status is InstallStatus.INSTALLED
    assert _status(engine, "datasette").status is InstallStatus.QUEUED
    assert engine.run_next_job()
    datasette = _status(engine, "datasette")
    assert datasette.status is InstallStatus.INSTALLED
    # Each install gets its own port.
    assert (_status(engine, "radicale").port, datasette.port) == (8080, 8081)


def test_requests_the_store_cannot_honour_are_refused_in_plain_words(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path, registry_rows=[make_registry_row("marimo", 9000)])
    with pytest.raises(InstallRefusedError, match="no app called 'nope'"):
        engine.request_install("nope")
    with pytest.raises(InstallRefusedError, match="already has an app called marimo"):
        engine.request_install("marimo")
    (engine.layout.dropin_path("radicale")).write_text("[program:radicale]\n")
    with pytest.raises(InstallRefusedError, match="already runs a program called radicale"):
        engine.request_install("radicale")
    engine.request_install("datasette")
    with pytest.raises(InstallRefusedError, match="already installed or being worked on"):
        engine.request_install("datasette")
    with pytest.raises(InstallRefusedError, match="not installed"):
        engine.request_uninstall("jupyterlab", is_deleting_data=False)
    with pytest.raises(InstallRefusedError, match="not installed, or is busy"):
        engine.request_uninstall("datasette", is_deleting_data=False)


def test_an_app_that_wont_fit_is_refused_with_the_reason(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=50, memory_available_mb=4_000)
    with pytest.raises(InstallRefusedError, match="JupyterLab needs about 160 MB of disk, and 50 MB is free"):
        engine.request_install("jupyterlab")
    assert engine.records.read().records == ()


def test_uninstall_keeping_data_removes_the_app_but_leaves_its_files(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, registrar, _probe = _fakes(engine)
    _install(engine, "datasette")
    (engine.layout.app_data_dir("datasette") / "mine.db").write_text("data")

    queued = engine.request_uninstall("datasette", is_deleting_data=False)
    assert (queued.status, queued.job) == (InstallStatus.QUEUED, JobKind.UNINSTALL)
    assert engine.run_next_job()

    assert engine.records.read().find("datasette") is None
    assert not engine.layout.dropin_path("datasette").exists()
    assert not engine.layout.env_dir("datasette").exists()
    assert not engine.layout.manifest_path("datasette").exists()
    assert (engine.layout.app_data_dir("datasette") / "mine.db").read_text() == "data"
    assert supervisor.programs == set()
    assert "stop datasette" in supervisor.calls
    assert registrar.unregistered == ["datasette"]


def test_uninstall_deleting_data_removes_the_whole_folder(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _install(engine, "datasette")
    engine.request_uninstall("datasette", is_deleting_data=True)
    engine.run_next_job()
    assert not engine.layout.app_dir("datasette").exists()


def test_a_failed_install_can_be_cleared_with_uninstall(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    installer, *_ = _fakes(engine)
    installer.is_failing = True
    _install(engine, "datasette")
    engine.request_uninstall("datasette", is_deleting_data=True)
    engine.run_next_job()
    assert engine.records.read().find("datasette") is None


def _put(
    engine: InstallEngine, slug: str, status: InstallStatus, job: JobKind | None, port: int | None = 8090
) -> None:
    engine.records.put(InstallRecord(slug=slug, version="x", port=port, status=status, job=job, updated_at=now_iso()))


def test_reconcile_undoes_an_install_a_restart_interrupted(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, registrar, _probe = _fakes(engine)
    _put(engine, "marimo", InstallStatus.CONFIGURING, JobKind.INSTALL)
    engine.records.enqueue("marimo")
    engine.layout.env_dir("marimo").mkdir(parents=True)
    engine.layout.dropin_path("marimo").write_text("# Generated by the App Store for marimo\n[program:marimo]\n")

    engine.reconcile()

    record = _status(engine, "marimo")
    assert (record.status, record.job, record.failure_reason) == (InstallStatus.FAILED, None, INTERRUPTED_REASON)
    assert engine.records.read().queue == ()
    assert not engine.layout.env_dir("marimo").exists()
    assert not engine.layout.dropin_path("marimo").exists()
    assert "stop marimo" in supervisor.calls
    assert registrar.unregistered == ["marimo"]


def test_reconcile_finishes_an_interrupted_uninstall_and_keeps_queued_jobs(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _put(engine, "radicale", InstallStatus.UNINSTALLING, JobKind.UNINSTALL)
    _put(engine, "datasette", InstallStatus.QUEUED, JobKind.INSTALL, port=None)
    _put(engine, "nodered", InstallStatus.DOWNLOADING, JobKind.REPAIR)
    engine.records.enqueue("ghost")

    engine.reconcile()

    queue = engine.records.read().queue
    assert set(queue) == {"radicale", "datasette", "nodered"}
    assert _status(engine, "radicale").status is InstallStatus.QUEUED
    assert _status(engine, "nodered").status is InstallStatus.QUEUED
    while engine.run_next_job():
        pass
    assert engine.records.read().find("radicale") is None
    assert _status(engine, "datasette").status is InstallStatus.INSTALLED
    assert _status(engine, "nodered").status is InstallStatus.INSTALLED


def test_reconcile_recreates_a_missing_dropin_for_an_installed_app(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, *_ = _fakes(engine)
    _install(engine, "datasette")
    engine.layout.dropin_path("datasette").unlink()
    supervisor.programs.clear()
    supervisor.calls.clear()

    engine.reconcile()

    assert "app-store-launch datasette" in engine.layout.dropin_path("datasette").read_text()
    assert supervisor.calls == ["apply datasette"]
    assert supervisor.programs == {"datasette"}
    # A drop-in already in place is left alone.
    supervisor.calls.clear()
    engine.reconcile()
    assert supervisor.calls == []


def test_reconcile_leaves_a_program_it_did_not_write_alone(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _installer, _runner, supervisor, *_ = _fakes(engine)
    _install(engine, "datasette")
    engine.layout.dropin_path("datasette").write_text("[program:datasette]\ncommand=mine\n")
    supervisor.calls.clear()

    engine.reconcile()

    assert engine.layout.dropin_path("datasette").read_text() == "[program:datasette]\ncommand=mine\n"
    assert supervisor.calls == []


def test_reconcile_queues_a_repair_when_an_installed_apps_environment_is_gone(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    installer, *_ = _fakes(engine)
    _install(engine, "datasette")
    (engine.layout.app_data_dir("datasette") / "mine.db").write_text("data")
    port = _status(engine, "datasette").port
    shutil.rmtree(engine.layout.env_dir("datasette"))

    engine.reconcile()
    record = _status(engine, "datasette")
    assert (record.status, record.job) == (InstallStatus.QUEUED, JobKind.REPAIR)
    assert engine.run_next_job()

    repaired = _status(engine, "datasette")
    assert (repaired.status, repaired.port) == (InstallStatus.INSTALLED, port)
    assert installer.installed == ["datasette==0.65.5", "datasette==0.65.5"]
    assert (engine.layout.app_data_dir("datasette") / "mine.db").read_text() == "data"


def test_a_record_for_an_app_no_longer_in_the_catalog_is_left_alone(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    _put(engine, "retired", InstallStatus.INSTALLED, None)
    engine.records.enqueue("retired")
    engine.reconcile()
    assert _status(engine, "retired").status is InstallStatus.INSTALLED
    # Its queued job is dropped rather than run.
    assert engine.run_next_job()
    assert engine.records.read().queue == ()


def test_the_worker_thread_runs_queued_jobs_and_stops(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    engine.start()
    try:
        engine.request_install("datasette")
        for _ in range(200):
            if _status(engine, "datasette").status is InstallStatus.INSTALLED:
                break
            engine._stopping.wait(0.02)
        assert _status(engine, "datasette").status is InstallStatus.INSTALLED
    finally:
        engine.stop()
    assert json.loads(engine.layout.records_path.read_text())["queue"] == []


class _RecordsWithAHookOnInstalled(RecordStore):
    """Runs ``on_installed`` once, right after a record is marked INSTALLED: the moment between a job's last record
    change and the worker taking it off the queue."""

    on_installed: Callable[[], object] | None = Field(default=None, description="What runs at that moment")

    def update(self, slug: str, **changes: object) -> InstallRecord:
        record = super().update(slug, **changes)
        if changes.get("status") is InstallStatus.INSTALLED and self.on_installed is not None:
            hook, self.on_installed = self.on_installed, None
            hook()
        return record


def test_a_request_landing_as_a_job_finishes_is_still_run(tmp_path: Path) -> None:
    base = build_test_engine(tmp_path)
    records = _RecordsWithAHookOnInstalled(path=base.layout.records_path)
    engine = base.model_copy(update={"records": records})
    records.on_installed = lambda: engine.request_uninstall("datasette", is_deleting_data=False)

    engine.request_install("datasette")
    assert engine.run_next_job()

    assert engine.records.read().queue == ("datasette",)
    assert _status(engine, "datasette").job is JobKind.UNINSTALL
    assert engine.run_next_job()
    assert engine.records.read().find("datasette") is None
    assert engine.records.read().queue == ()


class _MemoryReaderThatUninstalls(FakeProcessMemoryReader):
    """Removes ``uninstalled``'s record while its memory is read, as an uninstall finishing on the worker would."""

    records: RecordStore = Field(description="The records the uninstall changes")
    uninstalled: str = Field(description="The app uninstalled mid-sample")

    def pid_memory_mb(self, pid: int) -> int:
        if pid == self.program_pid(self.uninstalled):
            self.records.remove(self.uninstalled)
        return super().pid_memory_mb(pid)


def test_the_memory_sampler_carries_on_past_an_app_uninstalled_while_it_was_read(tmp_path: Path) -> None:
    base = build_test_engine(tmp_path)
    reader = _MemoryReaderThatUninstalls(
        records=base.records, uninstalled="datasette", memory_by_program={"datasette": 90, "radicale": 40}
    )
    engine = base.model_copy(update={"memory_reader": reader})
    _install(engine, "datasette")
    _install(engine, "radicale")

    engine.sample_memory_once(0.0)

    assert engine.records.read().find("datasette") is None
    assert _status(engine, "radicale").peak_memory_mb == 40
