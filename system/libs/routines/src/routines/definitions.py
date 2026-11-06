"""The definition files: reading every ``system/routines/<id>.toml``, and writing one with its comments kept."""

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

from imbue.imbue_common.pure import pure
from routines.data_types import DEFAULT_NOTIFY_AFTER_FAILURES
from routines.data_types import DEFAULT_RETRY_AFTER
from routines.data_types import DEFAULT_TIMEOUT
from routines.data_types import LoadedRoutine
from routines.data_types import RoutineDefinition
from routines.errors import DefinitionValidationError
from routines.errors import InvalidRoutineIdError
from routines.errors import RoutineExistsError
from routines.errors import RoutineNotFoundError
from routines.interfaces import DefinitionStoreInterface
from routines.interfaces import RepositoryInterface
from routines.paths import DEFINITION_SUFFIX
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


class FileDefinitionStore(DefinitionStoreInterface):
    """The definitions as files, each write committed through the repository."""

    repository: RepositoryInterface = Field(frozen=True, description="What commits each write")

    def list_routines(self) -> list[LoadedRoutine]:
        directory = self.paths.definitions_dir
        if not directory.is_dir():
            return []
        loaded: list[LoadedRoutine] = []
        for path in sorted(directory.glob(f"*{DEFINITION_SUFFIX}")):
            routine_id = _routine_id_of(path)
            if routine_id is None:
                logger.warning(
                    "Skipping a definition file whose name is not a routine id: {}",
                    path.name,
                )
                continue
            loaded.append(self._load_file(routine_id, path))
        return loaded

    def load(self, routine_id: RoutineId) -> LoadedRoutine:
        path = self.paths.definition_path(routine_id)
        if not path.is_file():
            raise RoutineNotFoundError(routine_id)
        return self._load_file(routine_id, path)

    def _load_file(self, routine_id: RoutineId, path: Path) -> LoadedRoutine:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return LoadedRoutine(
                routine_id=routine_id,
                definition=None,
                error=f"could not read the file: {e}",
            )
        try:
            definition = parse_definition(text)
        except tomllib.TOMLDecodeError as e:
            return LoadedRoutine(routine_id=routine_id, definition=None, error=f"not valid TOML: {e}")
        except ValidationError as e:
            return LoadedRoutine(
                routine_id=routine_id,
                definition=None,
                error=describe_validation_error(e),
            )
        return LoadedRoutine(routine_id=routine_id, definition=definition, error=None)

    def add(self, routine_id: RoutineId, definition: RoutineDefinition) -> Path:
        path = self.paths.definition_path(routine_id)
        if path.exists():
            raise RoutineExistsError(f"There is already a routine called {routine_id!r}.")
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
        path = self.paths.definition_path(routine_id)
        if not path.is_file():
            raise RoutineNotFoundError(routine_id)
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
        path = self.paths.definition_path(routine_id)
        if not path.is_file():
            raise RoutineNotFoundError(routine_id)
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
