"""The secret-request store: what an agent asked the user for, and the env file the answer lands in.

An agent that needs a credential runs the connect-external-service skill's
``request_secret.py``, which files a request here through ``POST /api/secret-requests``.
The chat renders the filed request as a card with one password input per variable;
on Submit the values are merged into ``data/.secrets/<file>.env`` (mode 0600, POSIX
single-quoted so any string survives ``source`` and ``with_secrets.py``), and the
agent is told the file and the variable names -- never the values, which touch no
log line, no exception message, and no persisted record.

Requests persist one JSON file each under ``data/.apps/chat/secret-requests/`` so a chat
app restart keeps a pending card live. A second pending request for the same file
supersedes the first: only the newest card accepts input.
"""

import os
import re
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self

from loguru import logger
from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import PrivateAttr
from pydantic import ValidationError
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# Both relative to the repo root, which is every chat app process's cwd.
DEFAULT_REQUESTS_DIRECTORY: Final[Path] = Path("data/.apps/chat/secret-requests")
DEFAULT_SECRETS_DIRECTORY: Final[Path] = Path("data/.secrets")
ENV_FILE_SUFFIX: Final[str] = ".env"

# The file name an agent may ask for: a short lowercase slug, so the path it becomes
# is predictable and cannot leave the directory.
SECRET_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# A POSIX shell identifier, which is what `source` accepts as a variable name.
SECRET_VARIABLE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_RATIONALE_LENGTH: Final[int] = 2000
MAX_NOTE_LENGTH: Final[int] = 2000
MAX_VARIABLES_PER_REQUEST: Final[int] = 16

_OWNER_ONLY_FILE_MODE: Final[int] = 0o600
_OWNER_ONLY_DIRECTORY_MODE: Final[int] = 0o700


class SecretRequestError(Exception):
    """Base class for everything the secret-request store refuses or cannot do."""


class InvalidSecretRequestError(SecretRequestError, ValueError):
    """Raised when a request's file name, variable names, or rationale are malformed."""


class UnknownSecretRequestError(SecretRequestError, KeyError):
    """Raised when no request has the given id."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"No secret request with id {request_id!r}")


class SecretRequestNotPendingError(SecretRequestError, ValueError):
    """Raised when a submit or decline reaches a request that has already been resolved."""

    def __init__(self, request_id: str, status: "SecretRequestStatus") -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"Secret request {request_id!r} is {status.value}, not pending")


class SecretValuesMismatchError(SecretRequestError, ValueError):
    """Raised when a submit names variables other than the ones the request asked for."""


class SecretFileWriteError(SecretRequestError, OSError):
    """Raised when the env file cannot be written; names the path and never a value."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        super().__init__(f"Could not write {path}: {reason}")


class NoticeDeliveryError(SecretRequestError):
    """Raised by the chat bridge when the chat's agent could not take a resolution notice."""


class ChatLookup(LowerCaseStrEnum):
    """What the router knows about a chat id when a request names it."""

    KNOWN = auto()
    UNKNOWN = auto()
    # The agent list has not been read yet, so nothing can be said either way.
    NOT_READY = auto()


class SecretRequestChatBridge(MutableModel, ABC):
    """What the secret-request routes borrow from the router: chat lookup and notice delivery."""

    @abstractmethod
    def lookup_chat(self, chat_id: str) -> ChatLookup:
        """Whether ``chat_id`` names a chat, or whether that cannot be known yet."""

    @abstractmethod
    def deliver_notice(self, chat_id: str, text: str) -> None:
        """Put a resolution notice into the chat's transcript; raises NoticeDeliveryError when its agent cannot take it."""


class SecretFileName(str):
    """The ``<file>`` of ``data/.secrets/<file>.env``: a lowercase slug."""

    def __new__(cls, value: str) -> Self:
        if not SECRET_FILE_NAME_PATTERN.match(value):
            raise InvalidSecretRequestError(
                f"file name {value!r} must be lowercase letters, digits and hyphens, starting with a letter or digit"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class SecretVariableName(str):
    """An environment variable name: a POSIX shell identifier."""

    def __new__(cls, value: str) -> Self:
        if not SECRET_VARIABLE_NAME_PATTERN.match(value):
            raise InvalidSecretRequestError(
                f"variable name {value!r} must be letters, digits and underscores, not starting with a digit"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


class SecretRequestId(RandomId):
    """Unique identifier for one secret request; the card and the resolution notice correlate on it."""

    PREFIX = "secret"


class SecretRequestStatus(LowerCaseStrEnum):
    """Where a request stands; the wire spelling the card and the resolution notice use."""

    PENDING = auto()
    STORED = auto()
    DECLINED = auto()
    SUPERSEDED = auto()


class SecretRequest(FrozenModel):
    """One filed request: what was asked for and how it was answered, never the answer itself."""

    request_id: SecretRequestId = Field(description="The id the card and the resolution notice share")
    chat_id: str = Field(description="The chat whose agent filed it, and whose transcript gets the notice")
    file: SecretFileName = Field(description="The <file> of data/.secrets/<file>.env")
    variables: tuple[SecretVariableName, ...] = Field(description="The variable names the card asks for, in order")
    rationale: str = Field(description="The agent's reason, shown on the card as its claim")
    status: SecretRequestStatus = Field(description="Pending until the user acts or a newer request supersedes it")
    filed_at: datetime = Field(description="When the request was filed")
    resolved_at: datetime | None = Field(default=None, description="When it left the pending state")
    superseded_by: SecretRequestId | None = Field(default=None, description="The newer request for the same file")
    note: str | None = Field(default=None, description="The user's optional note on a decline")

    @property
    def env_path(self) -> str:
        """The repo-relative path the card names and the notice reports."""
        return f"{DEFAULT_SECRETS_DIRECTORY.as_posix()}/{self.file}{ENV_FILE_SUFFIX}"


class FiledSecretRequest(FrozenModel):
    """The filing's answer: the request plus what it will do to the file that already exists."""

    request: SecretRequest = Field(description="The request as filed")
    existing_variables: tuple[SecretVariableName, ...] = Field(
        description="Variable names the env file already holds, so the card can say what a submit keeps"
    )
    overwrites: tuple[SecretVariableName, ...] = Field(
        description="The requested names the file already holds, which a submit replaces"
    )
    superseded: tuple[SecretRequest, ...] = Field(
        description="Pending requests for the same file that this one closed"
    )

    def as_wire(self) -> dict[str, Any]:
        """The flat object the request script prints and the card reads: the request's fields plus the two lists."""
        return {
            **self.request.model_dump(mode="json"),
            "env_path": self.request.env_path,
            "existing_variables": list(self.existing_variables),
            "overwrites": list(self.overwrites),
        }


class _EnvEntry(FrozenModel):
    """One span of an env file: a variable assignment (with its name) or a line kept verbatim."""

    name: str | None = Field(description="The variable set by this span, or None for a blank or comment line")
    text: str = Field(description="The span's exact text, including its trailing newline")


_ASSIGNMENT_START_RE: Final[re.Pattern[str]] = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


@pure
def quote_env_value(value: str) -> str:
    """A POSIX single-quoted string: every character literal, a quote spelled ``'\\''``."""
    return "'" + value.replace("'", "'\\''") + "'"


@pure
def _end_of_single_quoted_value(text: str, start: int) -> int:
    """The index just past a single-quoted value beginning at ``start``, honouring ``'\\''`` runs."""
    position = start
    while position < len(text) and text[position] == "'":
        closing = text.find("'", position + 1)
        if closing < 0:
            return len(text)
        position = closing + 1
        if text.startswith("\\'", position):
            position += 2
    return position


@pure
def split_env_entries(text: str) -> tuple[_EnvEntry, ...]:
    """The file as a sequence of spans, so a merge can replace an entry and keep everything else verbatim.

    Reads the format ``with_secrets.py`` reads: an assignment's value is a single-quoted
    string (which may span lines) or runs to the end of its line. Any other line is
    kept as it is.
    """
    entries: list[_EnvEntry] = []
    position = 0
    while position < len(text):
        line_end = text.find("\n", position)
        line_end = len(text) if line_end < 0 else line_end + 1
        assignment = _ASSIGNMENT_START_RE.match(text, position, line_end)
        if assignment is None:
            entries.append(_EnvEntry(name=None, text=text[position:line_end]))
            position = line_end
            continue
        value_start = assignment.end()
        if text.startswith("'", value_start):
            after_value = _end_of_single_quoted_value(text, value_start)
            entry_end = text.find("\n", after_value)
            entry_end = len(text) if entry_end < 0 else entry_end + 1
        else:
            entry_end = line_end
        entries.append(_EnvEntry(name=assignment.group(1), text=text[position:entry_end]))
        position = entry_end
    return tuple(entries)


@pure
def env_variable_names(text: str) -> tuple[SecretVariableName, ...]:
    """The variables an env file sets, in first-appearance order, without reading any value into the result."""
    names: list[SecretVariableName] = []
    for entry in split_env_entries(text):
        if entry.name is not None and entry.name not in names:
            names.append(SecretVariableName(entry.name))
    return tuple(names)


@pure
def merge_env_text(existing_text: str, value_by_name: Mapping[str, str]) -> str:
    """The file with the named variables set: each existing entry replaced in place, new ones appended, the rest kept."""
    remaining = dict(value_by_name)
    pieces: list[str] = []
    for entry in split_env_entries(existing_text):
        if entry.name is not None and entry.name in remaining:
            pieces.append(f"{entry.name}={quote_env_value(remaining.pop(entry.name))}\n")
        else:
            pieces.append(entry.text if entry.text.endswith("\n") else entry.text + "\n")
    for name, value in remaining.items():
        pieces.append(f"{name}={quote_env_value(value)}\n")
    return "".join(pieces)


class SecretRequestStore(MutableModel):
    """The requests on disk and the env files they write; the one writer of either."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    requests_directory: Path = Field(frozen=True, description="One JSON file per request lives here")
    secrets_directory: Path = Field(frozen=True, description="The data/.secrets/ the env files are written into")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def file_request(self, chat_id: str, file: str, variables: Sequence[str], rationale: str) -> FiledSecretRequest:
        """Record a new pending request, superseding any pending one for the same file.

        Raises InvalidSecretRequestError when the names or rationale are malformed.
        """
        file_name = SecretFileName(file)
        variable_names = _validated_variable_names(variables)
        if not rationale.strip():
            raise InvalidSecretRequestError("a rationale is required")
        if len(rationale) > MAX_RATIONALE_LENGTH:
            raise InvalidSecretRequestError(f"the rationale is longer than {MAX_RATIONALE_LENGTH} characters")
        now = _utc_now()
        request = SecretRequest(
            request_id=SecretRequestId(),
            chat_id=chat_id,
            file=file_name,
            variables=variable_names,
            rationale=rationale,
            status=SecretRequestStatus.PENDING,
            filed_at=now,
        )
        with self._lock:
            superseded = tuple(
                self._write(
                    pending.model_copy_update(
                        to_update(pending.field_ref().status, SecretRequestStatus.SUPERSEDED),
                        to_update(pending.field_ref().superseded_by, request.request_id),
                        to_update(pending.field_ref().resolved_at, now),
                    )
                )
                for pending in self._load_all()
                if pending.status is SecretRequestStatus.PENDING and pending.file == file_name
            )
            self._write(request)
            existing = env_variable_names(self._read_env_text(file_name))
        logger.debug(
            "Filed secret request {} for {} ({} variables)", request.request_id, request.env_path, len(variable_names)
        )
        return FiledSecretRequest(
            request=request,
            existing_variables=existing,
            overwrites=tuple(name for name in variable_names if name in existing),
            superseded=superseded,
        )

    def get(self, request_id: str) -> SecretRequest | None:
        with self._lock:
            return self._read(request_id)

    def submit(self, request_id: str, value_by_name: Mapping[str, str]) -> SecretRequest:
        """Write the values into the request's env file and mark it stored.

        The values are used for the write and nothing else: they are not kept on the
        returned request, not logged, and not part of any error this raises.
        Raises UnknownSecretRequestError, SecretRequestNotPendingError,
        SecretValuesMismatchError, or SecretFileWriteError.
        """
        with self._lock:
            request = self._pending(request_id)
            if set(value_by_name) != set(request.variables):
                raise SecretValuesMismatchError(
                    f"submit for {request_id!r} must name exactly {', '.join(request.variables)}"
                )
            if any(not value for value in value_by_name.values()):
                raise SecretValuesMismatchError(f"submit for {request_id!r} left a variable empty")
            self._write_env_file(request.file, value_by_name)
            stored = request.model_copy_update(
                to_update(request.field_ref().status, SecretRequestStatus.STORED),
                to_update(request.field_ref().resolved_at, _utc_now()),
            )
            self._write(stored)
        logger.debug("Stored secret request {} into {}", request_id, stored.env_path)
        return stored

    def decline(self, request_id: str, note: str | None) -> SecretRequest:
        """Mark the request declined, with the user's optional note.

        Raises UnknownSecretRequestError, SecretRequestNotPendingError, or
        InvalidSecretRequestError for an over-long note.
        """
        if note is not None and len(note) > MAX_NOTE_LENGTH:
            raise InvalidSecretRequestError(f"the note is longer than {MAX_NOTE_LENGTH} characters")
        with self._lock:
            request = self._pending(request_id)
            declined = request.model_copy_update(
                to_update(request.field_ref().status, SecretRequestStatus.DECLINED),
                to_update(request.field_ref().resolved_at, _utc_now()),
                to_update(request.field_ref().note, note or None),
            )
            self._write(declined)
        logger.debug("Declined secret request {}", request_id)
        return declined

    def _pending(self, request_id: str) -> SecretRequest:
        request = self._read(request_id)
        if request is None:
            raise UnknownSecretRequestError(request_id)
        if request.status is not SecretRequestStatus.PENDING:
            raise SecretRequestNotPendingError(request_id, request.status)
        return request

    def _request_path(self, request_id: str) -> Path:
        return self.requests_directory / f"{request_id}.json"

    def _read(self, request_id: str) -> SecretRequest | None:
        try:
            SecretRequestId(request_id)
        except ValueError:
            return None
        path = self._request_path(request_id)
        if not path.is_file():
            return None
        return self._load(path)

    def _load(self, path: Path) -> SecretRequest | None:
        try:
            return SecretRequest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as e:
            logger.warning("Skipping an unreadable secret request record {}: {}", path.name, e)
            return None

    def _load_all(self) -> list[SecretRequest]:
        if not self.requests_directory.is_dir():
            return []
        loaded = (self._load(path) for path in sorted(self.requests_directory.glob("*.json")))
        return [request for request in loaded if request is not None]

    def _write(self, request: SecretRequest) -> SecretRequest:
        self.requests_directory.mkdir(parents=True, exist_ok=True)
        path = self._request_path(request.request_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return request

    def _env_path(self, file: SecretFileName) -> Path:
        return self.secrets_directory / f"{file}{ENV_FILE_SUFFIX}"

    def _read_env_text(self, file: SecretFileName) -> str:
        path = self._env_path(file)
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise SecretFileWriteError(path, f"the existing file could not be read: {e.strerror}") from e

    def _write_env_file(self, file: SecretFileName, value_by_name: Mapping[str, str]) -> None:
        """Merge the values into the env file, atomically and owner-readable only."""
        path = self._env_path(file)
        merged = merge_env_text(self._read_env_text(file), value_by_name)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            self.secrets_directory.mkdir(parents=True, exist_ok=True, mode=_OWNER_ONLY_DIRECTORY_MODE)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY_FILE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(merged)
            os.chmod(temporary, _OWNER_ONLY_FILE_MODE)
            os.replace(temporary, path)
        except OSError as e:
            raise SecretFileWriteError(path, e.strerror or type(e).__name__) from e


def _validated_variable_names(variables: Sequence[str]) -> tuple[SecretVariableName, ...]:
    if not variables:
        raise InvalidSecretRequestError("at least one variable name is required")
    if len(variables) > MAX_VARIABLES_PER_REQUEST:
        raise InvalidSecretRequestError(f"at most {MAX_VARIABLES_PER_REQUEST} variables per request")
    names = tuple(SecretVariableName(name) for name in variables)
    if len(set(names)) != len(names):
        raise InvalidSecretRequestError("variable names must be distinct")
    return names


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
