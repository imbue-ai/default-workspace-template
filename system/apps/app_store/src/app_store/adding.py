"""The "Add app" flow: check a request, add it, and remove an added app from the store.

Check runs every rule that needs no network (``added_apps.check_request``), pins the version against the registry,
and estimates the disk (``package_check``) -- installing nothing. Add runs the same check again (the lookups are
cached), keeps the app in ``added-apps.json``, and queues its install through the engine like any other app's. Remove
takes an app out of the store again, once it is not installed; it deletes whatever the app left behind (its install
log, and any files kept from an uninstall). For an app added from PyPI or npm it also finds the newest version and
builds the entry an update to it will have: the same command and settings files at the new pin.

An added app's supervisord drop-in is generated like a curated one's, but its name is the user's, so it cannot be
listed in the tracked .gitignore: the store lists it in the repository's local ``info/exclude`` instead, in a block it
owns and rewrites from the added apps.
"""

import os
import shlex
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from app_store.added_apps import ADDED_APP_ICON
from app_store.added_apps import AddAppRequest
from app_store.added_apps import AddedApp
from app_store.added_apps import CheckedRequest
from app_store.added_apps import DEFAULT_PYTHON
from app_store.added_apps import SOURCE_NAMES
from app_store.added_apps import SetupFileText
from app_store.added_apps import check_package_request
from app_store.added_apps import check_request
from app_store.added_apps import taken_app_names
from app_store.catalog import CatalogApp
from app_store.catalog import LaunchCommand
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonRecipe
from app_store.catalog import ResourceEstimate
from app_store.catalog import SetupFile
from app_store.catalog import render_argv
from app_store.engine import InstallEngine
from app_store.errors import AddAppRefusedError
from app_store.errors import InstallRefusedError
from app_store.errors import PackageLookupError
from app_store.package_check import DiskEstimate
from app_store.package_check import PackageChecker
from app_store.package_check import PackageFacts
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import now_iso
from app_store.room import RoomCheck
from app_store.room import check_room
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# What the command preview fills its placeholders with.
SAMPLE_PORT: Final[int] = 8090
EXCLUDE_BLOCK_START: Final[str] = "# App Store: the programs of apps added by the user (written by the store)"
EXCLUDE_BLOCK_END: Final[str] = "# App Store: end"


class CheckResult(FrozenModel):
    """What "Check" answers: what will be installed, and what it will cost."""

    slug: str = Field(description="The app's internal name")
    title: str = Field(description="Its display name")
    source: str = Field(description="pypi or npm")
    registry: str = Field(description="The registry's name, e.g. PyPI")
    package: str = Field(description="The package's name")
    version: str = Field(description="The exact version that will be pinned")
    python: str | None = Field(description="The Python version, for a Python package")
    registry_url: str = Field(description="The package's page on its registry, at that version")
    summary: str = Field(description="The package's own one-line description; empty when it has none")
    license: str = Field(description="Its license as the registry gives it; empty when unknown")
    command: tuple[str, ...] = Field(description="The command as given, split: program, then arguments")
    command_preview: tuple[str, ...] = Field(description="The exact command that will run, with sample values")
    setup_files: tuple[SetupFile, ...] = Field(description="The settings files written before its first start")
    package_count: int | None = Field(description="How many packages it pulls in; null when unresolved")
    disk_mb: int | None = Field(description="The estimated disk in MB; null when unknown")
    disk_basis: str = Field(description="How the estimate was worked out, or why it is unknown")
    verdict: str = Field(description="FITS, TIGHT or WONT_FIT against the disk free now")
    blocked_reason: str = Field(description="Why it cannot be installed; empty when it can")


class Checked(FrozenModel):
    """A request checked against the registry: the request, the facts, and the estimate."""

    request: CheckedRequest = Field(description="The normalized request")
    facts: PackageFacts = Field(description="What the registry says about the pinned version")
    estimate: DiskEstimate = Field(description="The disk estimate")


class AppAdder(MutableModel):
    """Checks, adds and removes added apps, over the engine that installs them."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    engine: InstallEngine = Field(frozen=True, description="Installs, repairs and uninstalls every app")
    checker: PackageChecker = Field(frozen=True, description="Pins and estimates packages")
    exclude_path: Path | None = Field(frozen=True, description="The repository's local info/exclude; None for none")

    # Held while an app is added or removed, so two adds can never take the same name and the excludes block is
    # rewritten by one caller at a time.
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    def check(self, request: AddAppRequest) -> CheckResult:
        """What adding ``request`` would install and cost; raises AddAppRefusedError in plain words."""
        checked = self._checked(request)
        room = self._room(checked)
        sample_data_dir = self.engine.layout.app_data_dir(checked.request.slug)
        return CheckResult(
            slug=checked.request.slug,
            title=checked.request.title,
            source=checked.request.source,
            registry=SOURCE_NAMES[checked.request.source],
            package=checked.request.package,
            version=checked.facts.version,
            python=checked.request.python,
            registry_url=checked.facts.registry_url,
            summary=checked.facts.summary,
            license=checked.facts.license,
            command=checked.request.command,
            command_preview=tuple(
                render_argv(
                    checked.request.launch_argv,
                    SAMPLE_PORT,
                    sample_data_dir,
                    self.engine.layout.env_dir(checked.request.slug),
                )
            ),
            setup_files=checked.request.setup_files,
            package_count=checked.estimate.package_count,
            disk_mb=checked.estimate.disk_mb,
            disk_basis=checked.estimate.basis,
            verdict=room.verdict.value,
            blocked_reason=room.blocked_reason,
        )

    def add(self, request: AddAppRequest) -> InstallRecord:
        """Keep the app in the store and queue its install; raises AddAppRefusedError or InstallRefusedError."""
        checked = self._checked(request)
        return self.add_entry(
            AddedApp(
                app=catalog_entry(checked),
                source=checked.request.source,
                package=checked.request.package,
                command=checked.request.command,
                package_count=checked.estimate.package_count,
                registry_url=checked.facts.registry_url,
                added_at=now_iso(),
            )
        )

    def add_entry(self, added: AddedApp) -> InstallRecord:
        """Keep ``added`` in the store and queue its install; when the install is refused (InstallRefusedError),
        nothing was installed, so it is taken out of the store again. Raises AddAppRefusedError when something took
        its name since it was checked (another add that was confirmed at the same time)."""
        slug = added.app.slug
        with self._lock:
            if slug in self.taken_names():
                raise AddAppRefusedError(
                    f"Something already has the name {slug!r} inside the workspace; {added.app.title} wasn't added."
                )
            self.engine.added.add(added)
            self.sync_excludes()
            try:
                return self.engine.request_install(slug)
            except InstallRefusedError:
                self.engine.added.remove(slug)
                self.sync_excludes()
                raise

    def taken_names(self) -> set[str]:
        """Every name an added app may not take now."""
        return taken_app_names(
            {app.slug for app in self.engine.catalog.apps},
            {str(row.name) for row in self.engine.read_registry_rows()},
            self.engine.layout.dropin_dir,
        )

    def remove(self, slug: str) -> None:
        """Take an added app out of the store; refused while it is installed or being worked on."""
        with self._lock:
            self._remove(slug)

    def _remove(self, slug: str) -> None:
        if self.engine.added.find(slug) is None:
            raise AddAppRefusedError(f"There is no added app called {slug!r}.")
        if any(app.slug == slug for app in self.engine.curated.apps):
            # A stale entry the shipped catalog took the name of: the record and files are the shipped app's.
            self.engine.added.remove(slug)
            self.sync_excludes()
            logger.info("Removed added app {}, which a shipped app's name replaced, from the store", slug)
            return
        record = self.engine.records.read().find(slug)
        if record is not None and record.status is not InstallStatus.FAILED:
            raise AddAppRefusedError("Uninstall it first; then it can be removed from the store.")
        if record is not None:
            self.engine.records.remove(slug)
        shutil.rmtree(self.engine.layout.app_dir(slug), ignore_errors=True)
        self.engine.shortcuts.forget(slug)
        self.engine.added.remove(slug)
        self.sync_excludes()
        logger.info("Removed added app {} from the store", slug)

    def sync_excludes(self) -> None:
        """Rewrite the store's block of the repository's local excludes to list every added app's drop-in. The file
        is replaced in one step, so the person's own lines in it are never lost to a half-written file."""
        if self.exclude_path is None:
            return
        with self._lock:
            try:
                existing = self.exclude_path.read_text(encoding="utf-8") if self.exclude_path.exists() else ""
            except OSError as e:
                logger.warning("Could not read {}: {}", self.exclude_path, e)
                return
            lines = [f"/system/supervisord.conf.d/{entry.app.slug}.conf" for entry in self.engine.added.read()]
            text = replace_exclude_block(existing, lines)
            if text == existing:
                return
            try:
                write_text_atomic(self.exclude_path, text)
            except OSError as e:
                logger.warning("Could not write {}: {}", self.exclude_path, e)

    def newest_version(self, slug: str) -> str:
        """The newest version of an app the user added from PyPI or npm, as its registry has it; raises
        AddAppRefusedError in plain words when it is not such an app or the registry cannot be reached."""
        entry = self._registry_entry(slug)
        normalized = check_package_request(request_from_entry(entry, version=""), set())
        return self._pin(normalized).version

    def build_update(self, slug: str, version: str) -> AddedApp:
        """The entry an app the user added from PyPI or npm will have at ``version`` (empty for the newest): the
        same command and settings files, the new pin. Raises AddAppRefusedError in plain words."""
        entry = self._registry_entry(slug)
        normalized = check_request(request_from_entry(entry, version=version), set())
        facts = self._pin(normalized)
        estimate = self.checker.estimate(normalized, facts.version)
        app = catalog_entry(Checked(request=normalized, facts=facts, estimate=estimate))
        if app.slug != slug:
            raise AddAppRefusedError(f"{entry.app.title} would get another name if updated; add it again instead.")
        return entry.model_copy(
            update={"app": app, "package_count": estimate.package_count, "registry_url": facts.registry_url}
        )

    def _registry_entry(self, slug: str) -> AddedApp:
        entry = self.engine.added.find(slug)
        if entry is None or entry.source not in ("pypi", "npm"):
            raise AddAppRefusedError(f"{slug} isn't an app you added from PyPI or npm.")
        return entry

    def _pin(self, normalized: CheckedRequest) -> PackageFacts:
        try:
            return self.checker.pin(normalized)
        except PackageLookupError as e:
            raise AddAppRefusedError(
                f"{SOURCE_NAMES[normalized.source]} couldn't be reached to look the package up ({e}). Try again in "
                "a moment."
            ) from e

    def _checked(self, request: AddAppRequest) -> Checked:
        normalized = check_request(request, self.taken_names())
        facts = self._pin(normalized)
        estimate = self.checker.estimate(normalized, facts.version)
        return Checked(request=normalized, facts=facts, estimate=estimate)

    def _room(self, checked: Checked) -> RoomCheck:
        return check_room(checked.request.title, checked.estimate.disk_mb, None, self.engine.resources.read_room())


def request_from_entry(entry: AddedApp, version: str) -> AddAppRequest:
    """The request that added ``entry``, at ``version`` (empty for the newest)."""
    recipe = entry.app.recipe
    return AddAppRequest(
        source="pypi" if entry.source == "pypi" else "npm",
        package=entry.package,
        version=version,
        display_name=entry.app.title,
        command=shlex.join(entry.command),
        python=recipe.python if isinstance(recipe, PythonRecipe) else DEFAULT_PYTHON,
        setup_files=tuple(
            SetupFileText(path=setup_file.path, content=setup_file.content)
            for setup_file in entry.app.launch.setup_files
        ),
    )


def catalog_entry(checked: Checked) -> CatalogApp:
    """The catalog entry an added app installs and runs from."""
    request, facts = checked.request, checked.facts
    registry = SOURCE_NAMES[request.source]
    recipe = recipe_for(request, facts.version)
    summary = facts.summary or f"{request.package} from {registry}."
    return CatalogApp(
        slug=request.slug,
        tags=(),
        title=request.title,
        description=summary[:200],
        about=(
            f"{summary} Added by you from {registry} ({request.package} {facts.version}). Nobody has reviewed this "
            "package for the store."
        ),
        author=facts.author or "Unknown",
        license=facts.license or "Unknown",
        homepage_url=facts.homepage_url,
        source_url=facts.registry_url,
        version=facts.version,
        icon=ADDED_APP_ICON,
        recipe=recipe,
        launch=LaunchCommand(argv=request.launch_argv, setup_files=request.setup_files),
        resources=ResourceEstimate(disk_mb=checked.estimate.disk_mb, basis=checked.estimate.basis),
    )


def recipe_for(request: CheckedRequest, version: str) -> PythonRecipe | NodeRecipe:
    """The install recipe of an added package at its pinned version."""
    if request.source == "pypi":
        return PythonRecipe(
            kind="python", requirement=f"{request.package}=={version}", python=request.python or DEFAULT_PYTHON
        )
    return NodeRecipe(kind="node", package=f"{request.package}@{version}")


def replace_exclude_block(text: str, lines: list[str]) -> str:
    """``text`` with the store's block holding exactly ``lines`` (and no block when there are none)."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line == EXCLUDE_BLOCK_START:
            inside = True
            continue
        if inside and line == EXCLUDE_BLOCK_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    if lines:
        kept.extend([EXCLUDE_BLOCK_START, *lines, EXCLUDE_BLOCK_END])
    return "\n".join(kept) + ("\n" if kept else "")


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` through a same-folder temp file and a rename, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def local_exclude_path(repo_root: Path) -> Path | None:
    """The repository's local exclude file (``git rev-parse --git-path info/exclude``); None outside a repository."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not find the repository's exclude file: {}", e)
        return None
    if completed.returncode != 0 or completed.stdout.strip() == "":
        return None
    path = Path(completed.stdout.strip())
    return path if path.is_absolute() else repo_root / path
