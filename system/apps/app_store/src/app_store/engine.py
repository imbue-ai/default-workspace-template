"""The install engine: one job at a time, in a background thread, over persisted records.

An install moves QUEUED -> DOWNLOADING (its environment is made from the recipe) -> CONFIGURING (port, data folder,
manifest, icon, one-time init, drop-in) -> STARTING (supervisord picks the program up and the store waits for the
port to answer) -> INSTALLED, with the measured disk recorded. Any step that fails undoes what the install made (but
the log) and leaves the record FAILED with a plain reason. An uninstall stops the program, removes its drop-in and
its registry row, deletes the environment, and deletes the app's data only when asked to.

The apps it works on are the shipped catalog followed by the apps the user added (``added_apps.py``): one list, so
an added app takes exactly the same install, repair, uninstall and roll-back paths. After the package is installed,
the program the launch command runs must exist in the app's environment; an install whose package did not provide
it fails and rolls back, naming the programs it did provide. An app that never answers on its port fails the same
way, with the tail of its own output copied into the install log.

While an installed app runs, a sampler records the most memory its process tree holds during the first minutes of
each run (``peak_memory_mb``), which the page shows as measured.

At startup ``reconcile`` makes the world match the records: an install a restart interrupted is undone and marked
failed, an interrupted uninstall is finished, an installed app whose drop-in went missing (a rebuild) gets it back,
and one whose environment went missing (a restore that skips rebuildable folders) is queued for a repair.
"""

import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import load_manifest
from app_manifest.registry import RegistryRow
from app_store.added_apps import AddedApp
from app_store.added_apps import AddedAppStore
from app_store.added_apps import offered_catalog
from app_store.catalog import AppCatalog
from app_store.catalog import CatalogApp
from app_store.catalog import ICONS_DIRECTORY
from app_store.catalog import render_argv
from app_store.catalog import render_setup_content
from app_store.errors import InstallError
from app_store.errors import InstallRefusedError
from app_store.errors import PortError
from app_store.layout import INITIALIZED_MARKER
from app_store.layout import NO_BACKUP_MARKER
from app_store.layout import StoreLayout
from app_store.layout import is_store_dropin
from app_store.layout import render_dropin
from app_store.layout import render_manifest
from app_store.ports import is_port_bound
from app_store.ports import pick_free_port
from app_store.process_memory import ProcessMemoryReaderInterface
from app_store.records import IN_FLIGHT_STATUSES
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import RecordStore
from app_store.records import now_iso
from app_store.room import ResourceReaderInterface
from app_store.room import RoomVerdict
from app_store.room import check_app_room
from app_store.room import measure_directory_mb
from app_store.system import CommandRunnerInterface
from app_store.system import PackageInstallerInterface
from app_store.system import PortProbeInterface
from app_store.system import RegistrarInterface
from app_store.system import SupervisorInterface
from imbue.imbue_common.mutable_model import MutableModel

# How long a freshly installed app has to start answering on its port.
START_TIMEOUT_SECONDS: Final[float] = 120.0
INIT_TIMEOUT_SECONDS: Final[float] = 120.0
INTERRUPTED_REASON: Final[str] = "The install was interrupted when the App Store restarted. Try again."
UPDATE_INTERRUPTED_REASON: Final[str] = (
    "The update was interrupted when the App Store restarted, so the version you had was put back. Try again."
)

# How long into each run an app's memory is sampled, and how often.
MEMORY_SAMPLE_WINDOW_SECONDS: Final[float] = 180.0
MEMORY_SAMPLE_INTERVAL_SECONDS: Final[float] = 5.0
# How much of a failed app's own output is copied into its install log.
PROGRAM_LOG_TAIL_LINES: Final[int] = 40
PROGRAM_LOG_DIRECTORY: Final[Path] = Path("/var/log/supervisor")
# Files an environment's bin folder holds that are not the package's programs.
_ENVIRONMENT_FILES: Final[frozenset[str]] = frozenset(
    {
        "activate",
        "activate.bat",
        "activate.csh",
        "activate.fish",
        "activate.nu",
        "activate.ps1",
        "activate_this.py",
        "deactivate.bat",
        "pydoc.bat",
        "python",
        "python3",
        "pip",
        "pip3",
    }
)

# What a step can fail with; anything else is a bug and propagates.
_STEP_ERRORS: Final[tuple[type[Exception], ...]] = (InstallError, OSError, PortError, ManifestLoadError)

RegistryReader = Callable[[], list[RegistryRow]]


def installed_programs(bin_dir: Path) -> list[str]:
    """The programs a package installed into an environment's bin folder, by name, leaving out the environment's own
    files (the interpreter, pip, the activate scripts)."""
    return sorted(
        entry.name
        for entry in (bin_dir.iterdir() if bin_dir.is_dir() else [])
        if entry.name not in _ENVIRONMENT_FILES and not entry.name.startswith(("python", "activate", "."))
    )


class InstallEngine(MutableModel):
    """The store's installs: what is asked for, the queue, and the worker that runs it."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    curated: AppCatalog = Field(frozen=True, description="The shipped catalog")
    added: AddedAppStore = Field(frozen=True, description="The apps the user added")
    layout: StoreLayout = Field(frozen=True, description="Where the store works")
    records: RecordStore = Field(frozen=True, description="The persisted install records")
    installer: PackageInstallerInterface = Field(frozen=True, description="Installs a recipe")
    runner: CommandRunnerInterface = Field(frozen=True, description="Runs an app's one-time init")
    supervisor: SupervisorInterface = Field(frozen=True, description="Adds, removes and stops programs")
    registrar: RegistrarInterface = Field(frozen=True, description="Removes registry rows")
    probe: PortProbeInterface = Field(frozen=True, description="Waits for an app to answer")
    resources: ResourceReaderInterface = Field(frozen=True, description="Reads the room the workspace has")
    read_registry_rows: RegistryReader = Field(frozen=True, description="The app registry's rows")
    memory_reader: ProcessMemoryReaderInterface = Field(frozen=True, description="Reads what a running app uses")
    icons_directory: Path = Field(default=ICONS_DIRECTORY, frozen=True, description="The catalog's icons")
    start_timeout_seconds: float = Field(default=START_TIMEOUT_SECONDS, frozen=True, description="Start wait")
    is_bound: Callable[[int], bool] = Field(default=is_port_bound, frozen=True, description="Port-in-use check")
    program_log_directory: Path = Field(
        default=PROGRAM_LOG_DIRECTORY, frozen=True, description="Where supervisord writes each program's output"
    )

    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stopping: threading.Event = PrivateAttr(default_factory=threading.Event)
    _worker: threading.Thread | None = PrivateAttr(default=None)
    _request_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _sampler: threading.Thread | None = PrivateAttr(default=None)
    # Each sampled program's current run: its pid, and when it was first seen.
    _runs: dict[str, tuple[int, float]] = PrivateAttr(default_factory=dict)

    @property
    def catalog(self) -> AppCatalog:
        """Every app the store offers (``offered_catalog``): the shipped catalog, then the apps the user added."""
        return offered_catalog(self.curated, self.added.read())

    # The worker

    def start(self) -> None:
        """Reconcile with the records, then start the worker thread."""
        self.reconcile()
        self._worker = threading.Thread(target=self._work, name="app-store-installs", daemon=True)
        self._worker.start()
        self._sampler = threading.Thread(target=self._sample_memory, name="app-store-memory", daemon=True)
        self._sampler.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        for thread in (self._worker, self._sampler):
            if thread is not None:
                thread.join(timeout=5)

    def _work(self) -> None:
        while not self._stopping.is_set():
            if not self.run_next_job():
                self._wake.wait()
                self._wake.clear()

    def run_next_job(self) -> bool:
        """Run the job at the head of the queue; False when the queue is empty."""
        queue = self.records.read().queue
        if not queue:
            return False
        slug = queue[0]
        record = self.records.read().find(slug)
        app = self.catalog.find(slug)
        if record is None or record.job is None or app is None:
            logger.warning("Dropped queued job for {}: no record, no job, or no longer in the catalog", slug)
            self.records.dequeue(slug)
            return True
        match record.job:
            case JobKind.INSTALL | JobKind.REPAIR:
                self._run_install(app, record.job)
            case JobKind.UPDATE:
                self._run_update(app, record)
            case JobKind.UNINSTALL:
                self._run_uninstall(app, record.is_deleting_data)
        self.records.dequeue(slug)
        return True

    # What the page asks for

    def request_install(self, slug: str) -> InstallRecord:
        """Queue an install of ``slug``; raises InstallRefusedError, in plain words, when it cannot be started."""
        with self._request_lock:
            app = self._catalog_app_or_refuse(slug)
            record = self.records.read().find(slug)
            if record is not None and record.status is not InstallStatus.FAILED:
                raise InstallRefusedError(f"{app.title} is already installed or being worked on.")
            self._refuse_name_taken(app)
            room = check_app_room(app, self.resources.read_room())
            if room.verdict is RoomVerdict.WONT_FIT:
                raise InstallRefusedError(room.blocked_reason)
            queued = self.records.put(
                InstallRecord(
                    slug=slug,
                    version=app.version,
                    port=None,
                    status=InstallStatus.QUEUED,
                    job=JobKind.INSTALL,
                    updated_at=now_iso(),
                )
            )
            self.records.enqueue(slug)
        self._wake.set()
        return queued

    def request_update(self, slug: str, target: CatalogApp, entry: AddedApp | None = None) -> InstallRecord:
        """Queue an update of installed ``slug`` to ``target`` (``entry`` is the user's added entry as it will be once
        updated); raises InstallRefusedError, in plain words, when it cannot be started."""
        with self._request_lock:
            app = self._catalog_app_or_refuse(slug)
            if target.slug != slug or (entry is not None and entry.app != target):
                raise InstallRefusedError(f"That update isn't for {app.title}.")
            record = self.records.read().find(slug)
            if record is None or record.status is not InstallStatus.INSTALLED or record.job is not None:
                raise InstallRefusedError(f"{app.title} isn't installed, or is busy with another change.")
            if record.version == target.version and record.update_to is None and target == app:
                raise InstallRefusedError(f"{app.title} {target.version} is already installed.")
            room = check_app_room(target, self.resources.read_room())
            if room.verdict is RoomVerdict.WONT_FIT:
                raise InstallRefusedError(room.blocked_reason)
            queued = self.records.update(
                slug,
                status=InstallStatus.QUEUED,
                job=JobKind.UPDATE,
                update_to=target,
                update_entry=entry,
                failure_reason="",
            )
            self.records.enqueue(slug)
        self._wake.set()
        return queued

    def request_uninstall(self, slug: str, is_deleting_data: bool) -> InstallRecord:
        """Queue an uninstall of ``slug``; raises InstallRefusedError when it is not installed (or failed)."""
        with self._request_lock:
            app = self._catalog_app_or_refuse(slug)
            record = self.records.read().find(slug)
            if record is None or record.status not in (InstallStatus.INSTALLED, InstallStatus.FAILED):
                raise InstallRefusedError(f"{app.title} is not installed, or is busy with another change.")
            queued = self.records.update(
                slug, status=InstallStatus.QUEUED, job=JobKind.UNINSTALL, is_deleting_data=is_deleting_data
            )
            self.records.enqueue(slug)
        self._wake.set()
        return queued

    def _catalog_app_or_refuse(self, slug: str) -> CatalogApp:
        app = self.catalog.find(slug)
        if app is None:
            raise InstallRefusedError(f"There is no app called {slug!r} in the store.")
        return app

    def _refuse_name_taken(self, app: CatalogApp) -> None:
        """An app of the same name that the store did not install (a user's own) must not be replaced."""
        if any(str(row.name) == app.slug for row in self.read_registry_rows()):
            raise InstallRefusedError(
                f"This workspace already has an app called {app.slug}, so {app.title} can't be installed beside it."
            )
        dropin = self.layout.dropin_path(app.slug)
        if dropin.exists() and not is_store_dropin(dropin):
            raise InstallRefusedError(
                f"This workspace already runs a program called {app.slug}, so {app.title} can't be installed beside it."
            )

    # Install and repair

    def _run_install(self, app: CatalogApp, job: JobKind) -> None:
        slug = app.slug
        log_path = self.layout.log_path(slug)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"[app-store] {job.value.lower()} {app.title} {app.version} ({app.recipe.package_spec}) at {now_iso()}\n"
        )
        try:
            self._set_status(slug, InstallStatus.DOWNLOADING)
            env_dir = self.layout.env_dir(slug)
            shutil.rmtree(env_dir, ignore_errors=True)
            self.installer.install(app.recipe, env_dir, log_path)
            (env_dir / NO_BACKUP_MARKER).write_text("")
            self._check_program(app)

            self._set_status(slug, InstallStatus.CONFIGURING)
            port = self._port_for(slug)
            self.records.update(slug, port=port)
            self._write_app_files(app)
            self._write_setup_files(app, port, log_path)
            self._run_init_once(app, port, log_path)
            self._write_dropin(app)

            self._set_status(slug, InstallStatus.STARTING)
            self.supervisor.apply_program_change(slug)
            if not self.probe.wait_until_answering(port, self.start_timeout_seconds):
                self._copy_program_output(slug, log_path)
                raise InstallError(
                    f"{app.title} didn't start answering on its port within {self.start_timeout_seconds:.0f} "
                    "seconds. Its own output is at the end of the install log."
                )
        except _STEP_ERRORS as e:
            reason = str(e) if isinstance(e, InstallError) else f"Something went wrong while installing: {e}"
            self._append_log(log_path, f"[app-store] failed: {reason}")
            logger.warning("Install of {} failed: {}", slug, reason)
            self._undo_install(slug)
            self.records.update(
                slug, status=InstallStatus.FAILED, job=None, failure_reason=reason, measured_disk_mb=None
            )
            return
        measured = measure_directory_mb(self.layout.app_dir(slug))
        self._append_log(log_path, f"[app-store] installed; it takes {measured} MB")
        self.records.update(
            slug, status=InstallStatus.INSTALLED, job=None, failure_reason="", measured_disk_mb=measured
        )

    # Update

    def _run_update(self, current: CatalogApp, record: InstallRecord) -> None:
        """Replace the installed version with ``record.update_to``, keeping the app's data and port. The running
        version is stopped and its environment set aside; the new one is installed and started; if anything fails, the
        new one is removed, the old one put back and started again, and the record says so."""
        slug = current.slug
        target = record.update_to or current
        log_path = self.layout.log_path(slug)
        self._append_log(
            log_path,
            f"[app-store] update {current.title} {record.version} to {target.version} "
            f"({target.recipe.package_spec}) at {now_iso()}",
        )
        env_dir, previous = self.layout.env_dir(slug), self.layout.previous_env_dir(slug)
        dropin, manifest = self.layout.dropin_path(slug), self.layout.manifest_path(slug)
        saved_dropin = dropin.read_text(encoding="utf-8") if dropin.exists() else None
        saved_manifest = manifest.read_text(encoding="utf-8") if manifest.exists() else None
        try:
            self._set_status(slug, InstallStatus.DOWNLOADING)
            self.supervisor.stop(slug)
            shutil.rmtree(previous, ignore_errors=True)
            if env_dir.exists():
                env_dir.rename(previous)
            self.installer.install(target.recipe, env_dir, log_path)
            (env_dir / NO_BACKUP_MARKER).write_text("")
            self._check_program(target)

            self._set_status(slug, InstallStatus.CONFIGURING)
            port = record.port if record.port is not None else self._port_for(slug)
            self.records.update(slug, port=port)
            self._write_app_files(target)
            self._write_setup_files(target, port, log_path)
            self._run_init_once(target, port, log_path)
            self._write_dropin(target)

            self._set_status(slug, InstallStatus.STARTING)
            self.supervisor.apply_program_change(slug)
            self.supervisor.start(slug)
            if not self.probe.wait_until_answering(port, self.start_timeout_seconds):
                self._copy_program_output(slug, log_path)
                raise InstallError(
                    f"{target.title} {target.version} didn't start answering within "
                    f"{self.start_timeout_seconds:.0f} seconds. Its own output is at the end of the install log."
                )
        except _STEP_ERRORS as e:
            reason = str(e) if isinstance(e, InstallError) else f"Something went wrong while updating: {e}"
            self._append_log(log_path, f"[app-store] update failed: {reason}")
            logger.warning("Update of {} failed: {}", slug, reason)
            self._put_previous_back(slug, saved_dropin, saved_manifest, log_path)
            self.records.update(
                slug,
                status=InstallStatus.INSTALLED,
                job=None,
                update_to=None,
                update_entry=None,
                failure_reason=f"Updating to {target.version} didn't work, so {record.version} was put back: {reason}",
            )
            return
        shutil.rmtree(previous, ignore_errors=True)
        if record.update_entry is not None:
            self.added.add(record.update_entry)
        measured = measure_directory_mb(self.layout.app_dir(slug))
        self._append_log(log_path, f"[app-store] updated to {target.version}; it takes {measured} MB")
        self.records.update(
            slug,
            status=InstallStatus.INSTALLED,
            job=None,
            version=target.version,
            update_to=None,
            update_entry=None,
            failure_reason="",
            measured_disk_mb=measured,
        )

    def _put_previous_back(
        self, slug: str, saved_dropin: str | None, saved_manifest: str | None, log_path: Path
    ) -> None:
        """Undo a failed update: the version that was running, its drop-in and manifest, running again."""
        env_dir, previous = self.layout.env_dir(slug), self.layout.previous_env_dir(slug)
        try:
            self.supervisor.stop(slug)
            if previous.exists():
                shutil.rmtree(env_dir, ignore_errors=True)
                previous.rename(env_dir)
            if saved_manifest is not None:
                self.layout.manifest_path(slug).write_text(saved_manifest, encoding="utf-8")
            if saved_dropin is not None:
                self.layout.dropin_path(slug).write_text(saved_dropin, encoding="utf-8")
            self.supervisor.apply_program_change(slug)
            self.supervisor.start(slug)
            self._append_log(log_path, "[app-store] the version that was running was put back")
        except _STEP_ERRORS as e:
            logger.warning("Could not put {} back after a failed update: {}", slug, e)
            self._append_log(log_path, f"[app-store] putting the previous version back failed too: {e}")

    def _check_program(self, app: CatalogApp) -> None:
        """The launch command's program must be in the environment the install made; if not, name what is."""
        program = Path(render_argv(app.launch.argv[:1], 0, Path("/"), self.layout.env_dir(app.slug))[0])
        if program.is_file():
            return
        provided = installed_programs(program.parent)
        listing = ", ".join(provided) if provided else "none"
        raise InstallError(
            f"The package didn't install a program called {program.name!r}. The programs it did install: {listing}. "
            "Change the start command to use one of them and try again."
        )

    def _copy_program_output(self, slug: str, log_path: Path) -> None:
        """Copy the last lines of the program's own output into the install log, so its failure shows there."""
        for stream in ("stdout", "stderr"):
            source = self.program_log_directory / f"{slug}-{stream}.log"
            try:
                lines = source.read_text(encoding="utf-8", errors="replace").splitlines()[-PROGRAM_LOG_TAIL_LINES:]
            except OSError:
                continue
            self._append_log(log_path, f"[app-store] the last of {slug}'s {stream}:")
            for line in lines:
                self._append_log(log_path, f"    {line}")

    def _port_for(self, slug: str) -> int:
        record = self.records.read().find(slug)
        if record is not None and record.port is not None and not self.is_bound(record.port):
            return record.port
        held = [other.port for other in self.records.read().records if other.slug != slug and other.port is not None]
        return pick_free_port(
            self.layout.supervisord_conf, self.layout.repo_root / "data" / ".state" / "apps.toml", held, self.is_bound
        )

    def _write_app_files(self, app: CatalogApp) -> None:
        self.layout.app_data_dir(app.slug).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.icons_directory / app.icon, self.layout.icon_path(app.slug))
        manifest_path = self.layout.manifest_path(app.slug)
        manifest_path.write_text(render_manifest(app), encoding="utf-8")
        # Refused here rather than at registration, where a bad manifest would crash-loop the program.
        load_manifest(manifest_path)

    def _write_setup_files(self, app: CatalogApp, port: int, log_path: Path) -> None:
        """Write the app's settings files into its data folder; one already there (the person's own, or kept from an
        earlier install) is left as it is."""
        data_dir = self.layout.app_data_dir(app.slug)
        for setup_file in app.launch.setup_files:
            target = data_dir / setup_file.path
            if target.exists():
                self._append_log(log_path, f"[app-store] kept the existing {setup_file.path}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_setup_content(setup_file.content, port, data_dir), encoding="utf-8")
            self._append_log(log_path, f"[app-store] wrote {setup_file.path}")

    def _run_init_once(self, app: CatalogApp, port: int, log_path: Path) -> None:
        data_dir = self.layout.app_data_dir(app.slug)
        marker = data_dir / INITIALIZED_MARKER
        if not app.launch.init_argv or marker.exists():
            return
        argv = render_argv(app.launch.init_argv, port, data_dir, self.layout.env_dir(app.slug))
        # In the data folder, where the app itself runs and where a trial ran it.
        if self.runner.run(argv, log_path, INIT_TIMEOUT_SECONDS, cwd=data_dir) != 0:
            raise InstallError(f"Setting up {app.title}'s data folder failed (see the install log).")
        marker.write_text("")

    def _write_dropin(self, app: CatalogApp) -> None:
        self.layout.dropin_dir.mkdir(parents=True, exist_ok=True)
        self.layout.dropin_path(app.slug).write_text(render_dropin(app, self.layout.repo_root), encoding="utf-8")

    def _undo_install(self, slug: str) -> None:
        """Remove what a failed install made, keeping the app's data folder (it may predate this install) and the
        log."""
        self._remove_program(slug)
        shutil.rmtree(self.layout.env_dir(slug), ignore_errors=True)
        for path in (self.layout.manifest_path(slug), self.layout.icon_path(slug)):
            path.unlink(missing_ok=True)

    def _remove_program(self, slug: str) -> None:
        """Stop the program, delete its drop-in (if the store wrote it), tell supervisord, and drop the registry row."""
        self.supervisor.stop(slug)
        dropin = self.layout.dropin_path(slug)
        if dropin.exists() and is_store_dropin(dropin):
            dropin.unlink()
            try:
                self.supervisor.apply_program_change(slug)
            except InstallError as e:
                logger.warning("supervisord kept {} after its drop-in was removed: {}", slug, e)
        self.registrar.unregister(slug)

    # Uninstall

    def _run_uninstall(self, app: CatalogApp, is_deleting_data: bool) -> None:
        slug = app.slug
        self._set_status(slug, InstallStatus.UNINSTALLING)
        self._append_log(self.layout.log_path(slug), f"[app-store] uninstall at {now_iso()}")
        self._remove_program(slug)
        app_dir = self.layout.app_dir(slug)
        if is_deleting_data:
            shutil.rmtree(app_dir, ignore_errors=True)
        else:
            shutil.rmtree(self.layout.env_dir(slug), ignore_errors=True)
            for path in (self.layout.manifest_path(slug), self.layout.icon_path(slug)):
                path.unlink(missing_ok=True)
        self.records.remove(slug)
        logger.info("Uninstalled {} ({} its data)", slug, "with" if is_deleting_data else "keeping")

    # Memory

    def _sample_memory(self) -> None:
        while not self._stopping.wait(MEMORY_SAMPLE_INTERVAL_SECONDS):
            self.sample_memory_once(time.monotonic())

    def sample_memory_once(self, now: float) -> None:
        """Read the memory of every installed app that is in the first minutes of a run, and keep its peak."""
        for record in self.records.read().records:
            if record.status is not InstallStatus.INSTALLED:
                continue
            pid = self.memory_reader.program_pid(record.slug)
            if pid is None:
                self._runs.pop(record.slug, None)
                continue
            run = self._runs.get(record.slug)
            if run is None or run[0] != pid:
                run = (pid, now)
                self._runs[record.slug] = run
            if now - run[1] > MEMORY_SAMPLE_WINDOW_SECONDS:
                continue
            memory = self.memory_reader.pid_memory_mb(pid)
            if memory > 0 and (record.peak_memory_mb is None or memory > record.peak_memory_mb):
                self.records.update(record.slug, peak_memory_mb=memory)

    # Startup

    def reconcile(self) -> None:
        """Make the workspace match the records (see the module docstring)."""
        current = self.records.read()
        for slug in current.queue:
            if current.find(slug) is None:
                self.records.dequeue(slug)
        for record in current.records:
            app = self.catalog.find(record.slug)
            if app is None:
                logger.warning(
                    "Install record for {} names an app no longer in the catalog; left as it is", record.slug
                )
                continue
            if record.status in IN_FLIGHT_STATUSES or record.status is InstallStatus.QUEUED:
                self._reconcile_unfinished(app, record)
            elif record.status is InstallStatus.INSTALLED:
                self._reconcile_installed(app)

    def _reconcile_unfinished(self, app: CatalogApp, record: InstallRecord) -> None:
        if record.job is JobKind.UNINSTALL:
            # Uninstalling is safe to repeat: finish it.
            self.records.update(app.slug, status=InstallStatus.QUEUED)
            self.records.enqueue(app.slug)
        elif record.status is InstallStatus.QUEUED:
            # Nothing was done yet: it stays queued.
            self.records.enqueue(app.slug)
        elif record.job is JobKind.REPAIR:
            self.records.update(app.slug, status=InstallStatus.QUEUED)
            self.records.enqueue(app.slug)
        elif record.job is JobKind.UPDATE:
            # The old version is set aside in env.previous until the new one starts: put it back.
            logger.info("Putting {} back after an update a restart interrupted", app.slug)
            log_path = self.layout.log_path(app.slug)
            self._append_log(log_path, f"[app-store] {UPDATE_INTERRUPTED_REASON}")
            if self.layout.previous_env_dir(app.slug).exists():
                shutil.rmtree(self.layout.env_dir(app.slug), ignore_errors=True)
                self.layout.previous_env_dir(app.slug).rename(self.layout.env_dir(app.slug))
            self.records.update(
                app.slug,
                status=InstallStatus.INSTALLED,
                job=None,
                update_to=None,
                update_entry=None,
                failure_reason=UPDATE_INTERRUPTED_REASON,
            )
            self.records.dequeue(app.slug)
            # Its drop-in may already name the new version's command: make it the entry's again.
            self._reconcile_installed(app)
        else:
            logger.info("Undoing the install of {} that a restart interrupted", app.slug)
            self._append_log(self.layout.log_path(app.slug), f"[app-store] {INTERRUPTED_REASON}")
            self._undo_install(app.slug)
            self.records.update(app.slug, status=InstallStatus.FAILED, job=None, failure_reason=INTERRUPTED_REASON)
            self.records.dequeue(app.slug)

    def _reconcile_installed(self, app: CatalogApp) -> None:
        if not (self.layout.env_dir(app.slug) / NO_BACKUP_MARKER).exists():
            logger.info("The environment of {} is missing; queueing a repair", app.slug)
            self.records.update(app.slug, status=InstallStatus.QUEUED, job=JobKind.REPAIR)
            self.records.enqueue(app.slug)
            return
        dropin = self.layout.dropin_path(app.slug)
        expected = render_dropin(app, self.layout.repo_root)
        if dropin.exists() and dropin.read_text(encoding="utf-8") == expected:
            return
        if dropin.exists() and not is_store_dropin(dropin):
            logger.warning("A program called {} that the store did not write is in the way; left as it is", app.slug)
            return
        logger.info("Re-creating the supervisord drop-in for {}", app.slug)
        try:
            self._write_app_files(app)
            self._write_dropin(app)
            self.supervisor.apply_program_change(app.slug)
        except _STEP_ERRORS as e:
            logger.warning("Could not restore {}: {}", app.slug, e)

    # Helpers

    def _set_status(self, slug: str, status: InstallStatus) -> None:
        self.records.update(slug, status=status)

    @staticmethod
    def _append_log(log_path: Path, line: str) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError as e:
            logger.warning("Could not write to {}: {}", log_path, e)
