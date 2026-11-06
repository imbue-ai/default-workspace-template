"""Apps added from a GitHub repository: a chat works out how to install one and hands the store its install file
(``repo_install_file.py``); the store checks it, holds it as a *pending add*, and shows the person a confirmation.
Only their confirmation adds it -- as an ordinary added app, installed through the engine like any other -- so
whatever the chat tried, only a checked file the person agreed to becomes an app.

Pending adds are kept in ``pending-repo-adds.json`` in the store's data folder (written atomically, with a format
number), at most ``MAX_PENDING`` of them, the oldest dropped first; the screenshot each one's trial took is kept beside
it, so the confirmation shows the app as it ran. A pending add is never installed, and nothing is
left behind when it is discarded.
"""

import base64
import threading
import uuid
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from app_manifest.manifest import describe_validation_error
from app_store.added_apps import ADDED_APP_ICON
from app_store.added_apps import AddedApp
from app_store.added_apps import free_slug
from app_store.adding import AppAdder
from app_store.adding import SAMPLE_PORT
from app_store.catalog import CatalogApp
from app_store.catalog import render_argv
from app_store.errors import AddAppRefusedError
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import now_iso
from app_store.repo_install_file import RepoInstallFile
from app_store.room import RoomVerdict
from app_store.room import check_app_room
from app_store.system import install_steps
from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

PENDING_REPO_ADDS_FILENAME: Final[str] = "pending-repo-adds.json"
PENDING_REPO_ADDS_FORMAT: Final[int] = 1
# The screenshots a trial took, one PNG per pending add, in a folder beside the file.
SCREENSHOTS_DIRNAME: Final[str] = "pending-repo-add-screenshots"
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES: Final[int] = 5 * 1024 * 1024
MAX_PENDING: Final[int] = 20
DEFAULT_BASIS: Final[str] = "Measured by the chat that worked out how to install it, while it tried the app out."


class RepoAddSubmission(FrozenModel):
    """What a chat sends: the install file it worked out, and which chat it is."""

    install_file: RepoInstallFile = Field(description="How to install and start the repository")
    chat_id: str = Field(default="", description="The chat that worked it out, so the confirmation can link to it")
    replaces: str = Field(
        default="", description="For an update: the installed app (added from GitHub) this newer version replaces"
    )
    screenshot_png_base64: str = Field(
        default="", description="The trial's screenshot of the app inside a frame, a base64 PNG; empty for none"
    )


class PendingRepoAdd(FrozenModel):
    """An install file waiting for the person to confirm it."""

    id: str = Field(description="The pending add's id")
    slug: str = Field(description="The name the app will have")
    install_file: RepoInstallFile = Field(description="The install file, as the chat sent it")
    chat_id: str = Field(description="The chat that worked it out; empty when unknown")
    submitted_at: str = Field(description="When the chat sent it, ISO 8601 UTC")
    replaces: str = Field(default="", description="For an update: the installed app it replaces; empty for an add")


class PendingRepoAddView(FrozenModel):
    """A pending add as the confirmation shows it: what it is, exactly what will run, and whether it fits."""

    id: str = Field(description="The pending add's id")
    slug: str = Field(description="The name the app will have")
    title: str = Field(description="The name users see")
    description: str = Field(description="One line on what it is for")
    about: str = Field(description="A short paragraph on what it is")
    repository: str = Field(description="The repository's owner/name")
    repository_url: str = Field(description="The repository's page")
    commit: str = Field(description="The full commit hash it installs")
    commit_url: str = Field(description="The commit's page on GitHub")
    source_label: str = Field(description="What kind of install it is, e.g. 'Python package from GitHub'")
    install_commands: tuple[str, ...] = Field(description="The install's commands, in order, as they will run")
    start_command: str = Field(description="The command that starts it, with a sample port")
    disk_mb: int | None = Field(description="Disk it takes once installed, in MB; null when unknown")
    memory_in_use_mb: int | None = Field(description="Memory it holds while used, in MB; null when unknown")
    build_disk_mb: int | None = Field(description="Disk the install needs while it builds, in MB; null when not built")
    build_memory_mb: int | None = Field(description="Memory the build needs, in MB; null when not built")
    basis: str = Field(description="How it was worked out and measured, in plain words")
    verdict: RoomVerdict = Field(description="Whether it fits in the room the workspace has now")
    blocked_reason: str = Field(description="Why it cannot be added now; empty when it can")
    chat_id: str = Field(description="The chat that worked it out; empty when unknown")
    submitted_at: str = Field(description="When the chat sent it")
    replaces: str = Field(description="For an update: the installed app it replaces; empty for an add")
    installed_version: str = Field(description="For an update: the version installed now; empty for an add")
    screenshot_url: str | None = Field(description="The trial's screenshot of the app running; null when none")
    version: str = Field(description="The version it installs")


class _PendingDocument(FrozenModel):
    format: int = Field(description="The file's format")
    pending: list[object] = Field(default_factory=list, description="The pending adds, each validated on its own")


class PendingRepoAddStore(MutableModel):
    """The pending adds, kept in ``pending-repo-adds.json``. An entry that does not validate is skipped with a
    warning. Thread-safe."""

    path: Path = Field(frozen=True, description="The pending-repo-adds.json file")

    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    def read(self) -> tuple[PendingRepoAdd, ...]:
        with self._lock:
            return tuple(self._load())

    def find(self, pending_id: str) -> PendingRepoAdd | None:
        return next((pending for pending in self.read() if pending.id == pending_id), None)

    def add(self, pending: PendingRepoAdd, screenshot: bytes | None = None) -> None:
        with self._lock:
            if screenshot is not None:
                self.screenshots_dir.mkdir(parents=True, exist_ok=True)
                self.screenshot_path(pending.id).write_bytes(screenshot)
            entries = [*self._load(), pending]
            for dropped in entries[:-MAX_PENDING]:
                self.screenshot_path(dropped.id).unlink(missing_ok=True)
            self._save(entries[-MAX_PENDING:])

    def remove(self, pending_id: str) -> None:
        with self._lock:
            self._save([entry for entry in self._load() if entry.id != pending_id])
            self.screenshot_path(pending_id).unlink(missing_ok=True)

    @property
    def screenshots_dir(self) -> Path:
        return self.path.parent / SCREENSHOTS_DIRNAME

    def screenshot_path(self, pending_id: str) -> Path:
        return self.screenshots_dir / f"{pending_id}.png"

    def screenshot(self, pending_id: str) -> bytes | None:
        """The trial's screenshot of a pending add; None when it has none."""
        if self.find(pending_id) is None:
            return None
        try:
            return self.screenshot_path(pending_id).read_bytes()
        except OSError:
            return None

    def _load(self) -> list[PendingRepoAdd]:
        try:
            raw = read_json_object(self.path)
        except StateFileError as e:
            logger.warning("Ignored the pending adds in {}: {}", self.path, e)
            return []
        if raw is None:
            return []
        try:
            document = _PendingDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored the pending adds in {}: {}", self.path, e)
            return []
        if document.format != PENDING_REPO_ADDS_FORMAT:
            logger.warning(
                "Ignored the pending adds in {}: format {} is not {}",
                self.path,
                document.format,
                PENDING_REPO_ADDS_FORMAT,
            )
            return []
        entries: list[PendingRepoAdd] = []
        for item in document.pending:
            try:
                entries.append(PendingRepoAdd.model_validate(item))
            except ValidationError as e:
                logger.warning("Skipped a pending add in {}: {}", self.path, e)
        return entries

    def _save(self, entries: list[PendingRepoAdd]) -> None:
        write_json_atomic(
            self.path,
            {"format": PENDING_REPO_ADDS_FORMAT, "pending": [entry.model_dump(mode="json") for entry in entries]},
        )


def decode_screenshot(encoded: str) -> bytes | None:
    """The PNG a chat sent; None when it sent none. Raises AddAppRefusedError for anything but a PNG of sane size."""
    if encoded == "":
        return None
    try:
        image = base64.b64decode(encoded, validate=True)
    except ValueError as e:
        raise AddAppRefusedError("The screenshot isn't valid base64.") from e
    if not image.startswith(PNG_SIGNATURE):
        raise AddAppRefusedError("The screenshot isn't a PNG image.")
    if len(image) > MAX_SCREENSHOT_BYTES:
        raise AddAppRefusedError("The screenshot is too large (at most 5 MB).")
    return image


def repo_catalog_entry(install_file: RepoInstallFile, slug: str) -> CatalogApp:
    """The catalog entry an app added from a repository installs and runs from."""
    owner, _name = install_file.owner_and_name.split("/", 1)
    lead = install_file.about or install_file.description
    return CatalogApp(
        slug=slug,
        tags=(),
        title=install_file.title,
        description=install_file.description,
        about=(
            f"{lead} Added by you from GitHub ({install_file.owner_and_name} at {install_file.short_commit}). "
            "Nobody has reviewed it for the store."
        ),
        author=install_file.author or owner,
        license=install_file.license,
        homepage_url=install_file.repository,
        source_url=install_file.tree_url,
        version=install_file.recipe.pinned_version,
        icon=ADDED_APP_ICON,
        recipe=install_file.recipe,
        launch=install_file.launch,
        resources=install_file.resources.model_copy(
            update={"basis": install_file.basis or install_file.resources.basis or DEFAULT_BASIS}
        ),
    )


class RepoAdds(MutableModel):
    """Takes install files from chats, shows them for confirmation, and adds the confirmed ones."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    adder: AppAdder = Field(frozen=True, description="Keeps added apps and their excludes, over the engine")
    pending: PendingRepoAddStore = Field(frozen=True, description="The install files waiting for confirmation")

    def submit(self, submission: RepoAddSubmission) -> PendingRepoAdd:
        """Hold ``submission`` for confirmation; raises AddAppRefusedError in plain words when it cannot be added
        (its name is taken, or the store already offers that name)."""
        slug = (
            self._updatable_slug(submission.replaces, submission.install_file)
            if submission.replaces
            else self._free_slug(submission.install_file.title)
        )
        # Built now so a file the catalog would refuse is refused before anyone is asked to confirm it.
        try:
            repo_catalog_entry(submission.install_file, slug)
        except ValidationError as e:
            raise AddAppRefusedError(describe_validation_error(e)) from e
        screenshot = decode_screenshot(submission.screenshot_png_base64)
        pending = PendingRepoAdd(
            id=uuid.uuid4().hex[:12],
            slug=slug,
            install_file=submission.install_file,
            chat_id=submission.chat_id,
            submitted_at=now_iso(),
            replaces=submission.replaces,
        )
        self.pending.add(pending, screenshot)
        logger.info("Holding {} from {} for confirmation as {}", slug, submission.install_file.repository, pending.id)
        return pending

    def view(self, pending: PendingRepoAdd) -> PendingRepoAddView:
        engine = self.adder.engine
        install_file = pending.install_file
        app = repo_catalog_entry(install_file, pending.slug)
        env_dir = engine.layout.env_dir(pending.slug)
        check = check_app_room(app, engine.resources.read_room())
        return PendingRepoAddView(
            id=pending.id,
            slug=pending.slug,
            title=install_file.title,
            description=install_file.description,
            about=install_file.about or install_file.description,
            repository=install_file.owner_and_name,
            repository_url=install_file.repository,
            commit=install_file.commit,
            commit_url=install_file.commit_url,
            source_label=install_file.recipe.source_label,
            install_commands=tuple(" ".join(step.argv) for step in install_steps(install_file.recipe, env_dir)),
            start_command=" ".join(
                render_argv(install_file.launch.argv, SAMPLE_PORT, engine.layout.app_data_dir(pending.slug), env_dir)
            ),
            disk_mb=app.resources.disk_mb,
            memory_in_use_mb=app.resources.memory_in_use_mb,
            build_disk_mb=app.resources.build_disk_mb,
            build_memory_mb=app.resources.build_memory_mb,
            basis=app.resources.basis,
            verdict=check.verdict,
            blocked_reason=check.blocked_reason,
            chat_id=pending.chat_id,
            submitted_at=pending.submitted_at,
            replaces=pending.replaces,
            installed_version=self._installed_version(pending.replaces),
            screenshot_url=f"/api/repo-adds/{pending.id}/screenshot.png"
            if self.pending.screenshot_path(pending.id).is_file()
            else None,
            version=app.version,
        )

    def confirm(self, pending_id: str) -> InstallRecord:
        """Add the pending app to the store and queue its install; raises AddAppRefusedError or
        InstallRefusedError."""
        pending = self.pending.find(pending_id)
        if pending is None:
            raise AddAppRefusedError("That app is no longer waiting to be added.")
        if pending.replaces:
            return self._confirm_update(pending)
        # Checked again: something may have taken the name since the chat sent it.
        slug = self._free_slug(pending.install_file.title)
        install_file = pending.install_file
        record = self.adder.add_entry(
            AddedApp(
                app=repo_catalog_entry(install_file, slug),
                source="github",
                package=install_file.owner_and_name,
                command=install_file.launch.argv,
                package_count=None,
                registry_url=install_file.commit_url,
                added_at=now_iso(),
                repo_file=install_file,
                chat_id=pending.chat_id,
            )
        )
        self.pending.remove(pending_id)
        return record

    def _confirm_update(self, pending: PendingRepoAdd) -> InstallRecord:
        """Queue the update a pending add stands for: the app keeps its name, data and port; its entry becomes the new
        version's once the update succeeds."""
        slug = self._updatable_slug(pending.replaces, pending.install_file)
        current = self.adder.engine.added.find(slug)
        assert current is not None, "checked by _updatable_slug"
        install_file = pending.install_file
        entry = current.model_copy(
            update={
                "app": repo_catalog_entry(install_file, slug),
                "package": install_file.owner_and_name,
                "command": install_file.launch.argv,
                "registry_url": install_file.commit_url,
                "repo_file": install_file,
                "chat_id": pending.chat_id or current.chat_id,
            }
        )
        record = self.adder.engine.request_update(slug, entry.app, entry)
        self.pending.remove(pending.id)
        return record

    def _updatable_slug(self, slug: str, install_file: RepoInstallFile) -> str:
        """``slug`` when it is an installed app added from the same GitHub repository; AddAppRefusedError otherwise."""
        engine = self.adder.engine
        current = engine.added.find(slug)
        if current is None or current.source != "github" or current.repo_file is None:
            raise AddAppRefusedError(f"{slug} isn't an app you added from GitHub.")
        if current.repo_file.repository != install_file.repository:
            raise AddAppRefusedError(
                f"{current.app.title} comes from {current.repo_file.repository}, not {install_file.repository}."
            )
        if current.repo_file.commit == install_file.commit:
            raise AddAppRefusedError(f"{current.app.title} is already at that version.")
        record = engine.records.read().find(slug)
        if record is None or record.status is not InstallStatus.INSTALLED:
            raise AddAppRefusedError(f"{current.app.title} isn't installed, so there is nothing to update.")
        return slug

    def _installed_version(self, slug: str) -> str:
        if not slug:
            return ""
        record = self.adder.engine.records.read().find(slug)
        return record.version if record is not None else ""

    def discard(self, pending_id: str) -> None:
        if self.pending.find(pending_id) is None:
            raise AddAppRefusedError("That app is no longer waiting to be added.")
        self.pending.remove(pending_id)

    def _free_slug(self, title: str) -> str:
        return free_slug(title, self.adder.taken_names(), "Give it another title.")
