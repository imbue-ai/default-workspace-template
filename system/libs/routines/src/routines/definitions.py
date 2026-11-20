"""The definition files: reading every ``system/routines/<id>.toml`` and every
``system/apps/<package>/routines/<id>.toml``, and writing one with its comments kept.

A file under ``system/routines`` is the workspace's own, and the library rewrites and deletes it. A file under an
app's directory belongs to the app, which may be installed from someone else's repository: the library reads it,
writes a new one there when a routine is added for that app, and never rewrites or deletes one, since that would
leave the app modified against where it came from.
"""

import tomllib
from datetime import datetime
from pathlib import Path
from typing import Final

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from tomlkit import TOMLDocument
from tomlkit.exceptions import ParseError

from app_manifest.errors import InvalidManifestValueError
from app_manifest.primitives import AppName
from app_manifest.records import record_path
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from routines.data_types import DEFAULT_NOTIFY_AFTER_FAILURES
from routines.data_types import DEFAULT_RETRY_AFTER
from routines.data_types import DEFAULT_TIMEOUT
from routines.data_types import LoadedRoutine
from routines.data_types import RoutineDefinition
from routines.errors import AppOwnedRoutineError
from routines.errors import DefinitionValidationError
from routines.errors import DuplicateRoutineError
from routines.errors import InvalidRoutineIdError
from routines.errors import RoutineExistsError
from routines.errors import RoutineNotFoundError
from routines.interfaces import DefinitionStoreInterface
from routines.interfaces import RepositoryInterface
from routines.paths import APP_DEFINITIONS_DIRNAME
from routines.paths import DEFINITION_SUFFIX
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId

# The fields in the order a fresh file lists them; an edit keeps the file's own order and appends what is new.
_FIELD_ORDER: Final[tuple[str, ...]] = (
    "title",
    "what",
    "every",
    "at",
    "days",
    "day_of_month",
    "once",
    "command",
    "skill",
    "app",
    "enabled",
    "timeout",
    "retry_after",
    "notify_after_failures",
)
_ONCE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M"


@pure
def describe_validation_error(error: ValidationError) -> str:
    """The first problem of a failed validation in one line."""
    problems = error.errors()
    if not problems:
        return "the definition does not validate"
    first = problems[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "")).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


@pure
def definition_as_toml_values(definition: RoutineDefinition) -> dict[str, object]:
    """The definition's fields as a TOML file spells them, defaults left out."""
    values: dict[str, object] = {
        "title": str(definition.title),
        "what": str(definition.what),
    }
    if definition.every is not None:
        values["every"] = str(definition.every)
    if definition.at is not None:
        values["at"] = str(definition.at)
    if definition.days:
        values["days"] = [day.lower() for day in definition.days]
    if definition.day_of_month is not None:
        values["day_of_month"] = definition.day_of_month
    if definition.once is not None:
        values["once"] = definition.once.strftime(_ONCE_FORMAT)
    if definition.command is not None:
        values["command"] = list(definition.command)
    if definition.skill is not None:
        values["skill"] = str(definition.skill)
    if definition.app is not None:
        values["app"] = str(definition.app)
    if not definition.enabled:
        values["enabled"] = False
    if definition.timeout != DEFAULT_TIMEOUT:
        values["timeout"] = str(definition.timeout)
    if definition.retry_after != DEFAULT_RETRY_AFTER:
        values["retry_after"] = str(definition.retry_after)
    if definition.notify_after_failures != DEFAULT_NOTIFY_AFTER_FAILURES:
        values["notify_after_failures"] = int(definition.notify_after_failures)
    return values


@pure
def parse_definition(text: str) -> RoutineDefinition:
    """Raises tomllib's TOMLDecodeError or pydantic's ValidationError as they are: the caller words them."""
    raw = tomllib.loads(text)
    return RoutineDefinition.model_validate(raw)


@pure
def apply_values_to_document(document: TOMLDocument, values: dict[str, object]) -> TOMLDocument:
    """A copy of the document with the fields set in order and the absent ones removed; comments stay."""
    updated = tomlkit.parse(document.as_string())
    for field_name in _FIELD_ORDER:
        if field_name in values:
            updated[field_name] = values[field_name]
        elif field_name in updated:
            del updated[field_name]
    return updated


def _routine_id_of(path: Path) -> RoutineId | None:
    try:
        return RoutineId(path.name.removesuffix(DEFINITION_SUFFIX))
    except InvalidRoutineIdError:
        return None


class DefinitionFile(FrozenModel):
    """One definition file as found on disk."""

    routine_id: RoutineId = Field(description="The file's stem")
    path: Path = Field(description="The file")
    app_package: str | None = Field(description="The app directory the file is in; None under system/routines")


def user_app_package_of(paths: RoutinesPaths, app_name: str) -> str | None:
    """The directory under system/apps of the user app by that name; None when the name is no user app's. A user
    app has its directory and the workspace's record of it; a built-in app has no record, and its routines stay
    under system/routines."""
    try:
        name = AppName(app_name)
    except InvalidManifestValueError:
        return None
    app_package = name.replace("-", "_")
    if not paths.app_dir(app_package).is_dir() or not record_path(paths.repo_root, name).is_file():
        return None
    return app_package


@pure
def describe_duplicate(files: list[DefinitionFile], repo_root: Path) -> str:
    """What is wrong with an id that more than one file has, naming each file."""
    places = ", ".join(str(file.path.relative_to(repo_root)) for file in files)
    return (
        f"defined in more than one file ({places}); a routine id is unique across the workspace, so rename or "
        "delete all but one"
    )


class FileDefinitionStore(DefinitionStoreInterface):
    """The definitions as files, each write committed through the repository."""

    repository: RepositoryInterface = Field(frozen=True, description="What commits each write")

    def list_routines(self) -> list[LoadedRoutine]:
        files_by_id: dict[RoutineId, list[DefinitionFile]] = {}
        for file in self._all_files():
            files_by_id.setdefault(file.routine_id, []).append(file)
        return [self._load_files(files) for files in files_by_id.values()]

    def load(self, routine_id: RoutineId) -> LoadedRoutine:
        files = self._files_of(routine_id)
        if not files:
            raise RoutineNotFoundError(routine_id)
        return self._load_files(files)

    def _all_files(self) -> list[DefinitionFile]:
        """Every definition file, by file name, the workspace's own ahead of an app's of the same name."""
        candidates: list[tuple[Path, str | None]] = [
            (path, None) for path in self.paths.definitions_dir.glob(f"*{DEFINITION_SUFFIX}")
        ]
        candidates.extend(
            (path, path.parent.parent.name)
            for path in self.paths.apps_dir.glob(f"*/{APP_DEFINITIONS_DIRNAME}/*{DEFINITION_SUFFIX}")
        )
        files: list[DefinitionFile] = []
        for path, app_package in candidates:
            routine_id = _routine_id_of(path)
            if routine_id is None:
                logger.warning(
                    "Skipping a definition file whose name is not a routine id: {}",
                    path.relative_to(self.paths.repo_root),
                )
                continue
            files.append(DefinitionFile(routine_id=routine_id, path=path, app_package=app_package))
        return sorted(files, key=lambda file: (file.path.name, file.app_package is not None, file.path))

    def _files_of(self, routine_id: RoutineId) -> list[DefinitionFile]:
        """The files that have the id, in the order the listing takes them; empty when none does."""
        files: list[DefinitionFile] = []
        own = self.paths.workspace_definition_path(routine_id)
        if own.is_file():
            files.append(DefinitionFile(routine_id=routine_id, path=own, app_package=None))
        in_apps = self.paths.apps_dir.glob(f"*/{APP_DEFINITIONS_DIRNAME}/{routine_id}{DEFINITION_SUFFIX}")
        files.extend(
            DefinitionFile(routine_id=routine_id, path=path, app_package=path.parent.parent.name)
            for path in sorted(in_apps)
            if path.is_file()
        )
        return files

    def _only_file_of(self, routine_id: RoutineId) -> DefinitionFile:
        """The one file the library may rewrite or delete for the id; raises RoutineNotFoundError when no file has
        it, DuplicateRoutineError when several do, and AppOwnedRoutineError when the file is an app's."""
        files = self._files_of(routine_id)
        if not files:
            raise RoutineNotFoundError(routine_id)
        if len(files) > 1:
            raise DuplicateRoutineError(f"{routine_id} is {describe_duplicate(files, self.paths.repo_root)}.")
        only = files[0]
        if only.app_package is not None:
            raise AppOwnedRoutineError(routine_id, only.app_package)
        return only

    def _load_files(self, files: list[DefinitionFile]) -> LoadedRoutine:
        """The routine the files of one id make: the definition of the only one, and a broken routine for several,
        since choosing one would run a routine nobody can tell from the other."""
        first = files[0]
        if len(files) > 1:
            return LoadedRoutine(
                routine_id=first.routine_id,
                definition=None,
                error=describe_duplicate(files, self.paths.repo_root),
                path=first.path,
                app_package=first.app_package,
                is_duplicated=True,
            )
        definition, error = self._read_file(first.path)
        return LoadedRoutine(
            routine_id=first.routine_id,
            definition=definition,
            error=error,
            path=first.path,
            app_package=first.app_package,
            is_duplicated=False,
        )

    def _read_file(self, path: Path) -> tuple[RoutineDefinition | None, str | None]:
        """The file's definition, or why it has none."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return None, f"could not read the file: {e}"
        try:
            return parse_definition(text), None
        except tomllib.TOMLDecodeError as e:
            return None, f"not valid TOML: {e}"
        except ValidationError as e:
            return None, describe_validation_error(e)

    def add(self, routine_id: RoutineId, definition: RoutineDefinition) -> Path:
        if self._files_of(routine_id):
            raise RoutineExistsError(f"There is already a routine called {routine_id!r}.")
        app_package = user_app_package_of(self.paths, definition.app) if definition.app is not None else None
        path = (
            self.paths.app_definition_path(app_package, routine_id)
            if app_package is not None
            else self.paths.workspace_definition_path(routine_id)
        )
        document = apply_values_to_document(tomlkit.document(), definition_as_toml_values(definition))
        self._write(path, document)
        self.repository.commit_path(path, f"routines: add {routine_id}")
        return path

    def replace(
        self,
        routine_id: RoutineId,
        definition: RoutineDefinition,
        change_description: str,
    ) -> Path:
        path = self._only_file_of(routine_id).path
        try:
            existing = tomlkit.parse(path.read_text(encoding="utf-8"))
        except ParseError as e:
            logger.warning(
                "Rewriting the definition of {} whole, comments included: the file no longer parses ({})",
                routine_id,
                e,
            )
            existing = tomlkit.document()
        document = apply_values_to_document(existing, definition_as_toml_values(definition))
        self._write(path, document)
        self.repository.commit_path(path, f"routines: {change_description} of {routine_id}")
        return path

    def remove(self, routine_id: RoutineId) -> None:
        path = self._only_file_of(routine_id).path
        path.unlink()
        self.repository.commit_path(path, f"routines: remove {routine_id}")

    def _write(self, path: Path, document: TOMLDocument) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.as_string(), encoding="utf-8")


@pure
def once_moment_from_text(text: str) -> datetime:
    """``2026-11-03T15:00`` as a naive local moment; raises DefinitionValidationError for any other spelling."""
    try:
        return datetime.strptime(text.strip(), _ONCE_FORMAT)
    except ValueError as e:
        raise DefinitionValidationError(f"once: {text!r} is not a moment written as YYYY-MM-DDTHH:MM") from e
