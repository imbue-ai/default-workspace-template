import re
from pathlib import Path
from typing import Any, Final, Self

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from app_manifest.errors import InvalidManifestValueError

# The app-name rule is the registration script's rule (system/scripts/forward_port.py,
# ``validate_service_name``): the name becomes the leading label of the app's origin
# hostname. A drift test in forward_port_test.py keeps the two identical.
APP_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")
MAX_APP_NAME_LENGTH: Final[int] = 32
# ``localhost`` and ``auth`` are origin labels. The rest are the first label of every
# standalone supervisord program with a hyphen in its name (``share-gateway``,
# ``app-watcher``, ``owner-exec``, ``vm-exec-register``, ``host-backup``,
# ``env-converge``): an app named after one would claim that program as its
# ``<name>-<role>`` sidecar. ``system/test_app_manifests.py`` keeps this set in step
# with ``system/supervisord.conf``.
RESERVED_APP_NAMES: Final[frozenset[str]] = frozenset(
    {"localhost", "auth", "share", "app", "owner", "vm", "host", "env"}
)
RESERVED_APP_NAME_PREFIXES: Final[tuple[str, ...]] = ("host-", "agent-")

MAX_DISPLAY_NAME_LENGTH: Final[int] = 64

ACTION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Where the shell reaches an app's instances API: loopback only, one port a socket can listen on.
INSTANCES_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^http://(?:127\.0\.0\.1|localhost):(?P<port>[0-9]{1,5})$"
)
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535

PRIORITY_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$"
)

ICON_SUFFIX: Final[str] = ".svg"


@pure
def describe_app_name_problem(name: str) -> str | None:
    """Return why ``name`` cannot be an app name, or None when it can."""
    if not APP_NAME_PATTERN.fullmatch(name):
        return (
            f"invalid app name {name!r}: names must be lowercase alphanumeric/underscore runs "
            "separated by single hyphens"
        )
    if len(name) > MAX_APP_NAME_LENGTH:
        return f"invalid app name {name!r}: names must be at most {MAX_APP_NAME_LENGTH} characters"
    for prefix in RESERVED_APP_NAME_PREFIXES:
        if name.startswith(prefix):
            return f"invalid app name {name!r}: the {prefix!r} prefix is reserved"
    if name in RESERVED_APP_NAMES:
        return f"invalid app name {name!r}: this name is reserved"
    return None


class AppName(str):
    """The registered name of an app: its origin label prefix, program name, and registry key."""

    def __new__(cls, value: str) -> Self:
        problem = describe_app_name_problem(value)
        if problem is not None:
            raise InvalidManifestValueError(problem)
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class DisplayName(str):
    """What users see for an app: non-empty, at most 64 characters."""

    def __new__(cls, value: str) -> Self:
        if not value.strip():
            raise InvalidManifestValueError("display_name must not be empty")
        if len(value) > MAX_DISPLAY_NAME_LENGTH:
            raise InvalidManifestValueError(
                f"display_name must be at most {MAX_DISPLAY_NAME_LENGTH} characters, got {len(value)}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class ActionId(str):
    """The id of an action an app declares: lowercase, starts alphanumeric, at most 32 characters."""

    def __new__(cls, value: str) -> Self:
        if not ACTION_ID_PATTERN.fullmatch(value):
            raise InvalidManifestValueError(
                f"invalid action id {value!r}: ids match ^[a-z0-9][a-z0-9-]{{0,31}}$ (lowercase, no leading hyphen)"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class InstancesUrl(str):
    """Where the shell reaches an app's instances API: a loopback origin with a port."""

    def __new__(cls, value: str) -> Self:
        match = INSTANCES_URL_PATTERN.fullmatch(value)
        if match is None:
            raise InvalidManifestValueError(
                f"invalid instances_url {value!r}: expected http://127.0.0.1:<port> or http://localhost:<port>"
            )
        if not MIN_PORT <= int(match.group("port")) <= MAX_PORT:
            raise InvalidManifestValueError(
                f"invalid instances_url {value!r}: the port must be between {MIN_PORT} and {MAX_PORT}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class PriorityName(str):
    """A memory-shedding band name: a key of ``oom_priority.bands.SERVICE_BANDS`` (``user`` by default)."""

    def __new__(cls, value: str) -> Self:
        if not PRIORITY_NAME_PATTERN.fullmatch(value):
            raise InvalidManifestValueError(
                f"invalid priority {value!r}: expected a band name such as 'user'"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class ProgramName(NonEmptyStr):
    """The supervisord program that runs an app."""

    def __new__(cls, value: str) -> Self:
        if not value or any(character.isspace() for character in value):
            raise InvalidManifestValueError(
                f"invalid program {value!r}: must be a non-empty name without whitespace"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class AppUrl(NonEmptyStr):
    """The URL an app is reachable at from inside the workspace, exactly as registered."""


class IconPath(str):
    """The manifest's icon file: a relative ``.svg`` path, resolved against the manifest's directory."""

    def __new__(cls, value: str) -> Self:
        path = Path(value)
        if not value or path.is_absolute():
            raise InvalidManifestValueError(
                f"invalid icon {value!r}: must be a path relative to the manifest"
            )
        if path.suffix.lower() != ICON_SUFFIX:
            raise InvalidManifestValueError(
                f"invalid icon {value!r}: must be an {ICON_SUFFIX} file"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


# A reference names one literal artifact, so a path that could match many is refused
# outright rather than expanded: the manifest is a declaration of ownership, and every
# entry has to be checkable against the tree exactly.
_GLOB_CHARACTERS: Final[tuple[str, ...]] = ("*", "?", "[", "]")

# Trees a reference may never name: the vendored subtrees are not this repo's to own,
# and everything under data/ is gitignored runtime state that no review or test pass
# should be pointed at.
_FORBIDDEN_REFERENCE_PREFIXES: Final[tuple[str, ...]] = ("system/vendor/", "data/")

_MAX_REFERENCE_NOTE_LENGTH: Final[int] = 200

# The gitignore prefix that re-includes a path an earlier pattern excluded.
_EXCLUDE_NEGATION_PREFIX: Final[str] = "!"


@pure
def _describe_repo_relative_path_problem(value: str, field_name: str) -> str | None:
    """Why ``value`` cannot be a repo-root-relative POSIX path, or None when it can."""
    if not value or not value.strip():
        return f"{field_name} must not be empty"
    if value.startswith("/"):
        return f"invalid {field_name} {value!r}: must be relative to the repo root, not absolute"
    if "\\" in value:
        return f"invalid {field_name} {value!r}: paths are POSIX, so backslashes are not allowed"
    # One trailing slash only says that the path names a directory. Every other hole -- a
    # '..' or '.' segment, or an empty one -- makes the path something git never reports a
    # changed file as, so nothing beneath it would ever be matched.
    segments = value.removesuffix("/").split("/")
    if ".." in segments:
        return f"invalid {field_name} {value!r}: '..' segments are not allowed"
    if "." in segments:
        return f"invalid {field_name} {value!r}: '.' segments are not allowed"
    if "" in segments:
        return f"invalid {field_name} {value!r}: empty segments are not allowed"
    return None


@pure
def is_path_covered_by(footprint_entry: str, candidate: str) -> bool:
    """Whether ``candidate`` is the footprint entry itself or sits beneath it.

    A trailing slash on the entry only says that it names a directory, so it is ignored.
    """
    entry = footprint_entry.rstrip("/")
    return candidate == entry or candidate.startswith(f"{entry}/")


class RepoRelativePath(str):
    """A POSIX path relative to the repo root, as a creation's footprint names its parts.

    Each subclass narrows the rule by overriding ``_describe_problem``; the validation and
    the error it raises live here once.
    """

    def __new__(cls, value: str) -> Self:
        problem = cls._describe_problem(value)
        if problem is not None:
            raise InvalidManifestValueError(problem)
        return super().__new__(cls, value)

    @classmethod
    def _describe_problem(cls, value: str) -> str | None:
        return _describe_repo_relative_path_problem(value, "path")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class ReferencePath(RepoRelativePath):
    """A literal repo-root-relative file or directory that an app declares it owns."""

    @classmethod
    def _describe_problem(cls, value: str) -> str | None:
        problem = _describe_repo_relative_path_problem(value, "reference path")
        if problem is not None:
            return problem
        if value.endswith("/"):
            return f"invalid reference path {value!r}: name a directory without a trailing slash"
        for character in _GLOB_CHARACTERS:
            if character in value:
                return (
                    f"invalid reference path {value!r}: a reference is a literal path, but this "
                    f"contains the glob character {character!r}"
                )
        for prefix in _FORBIDDEN_REFERENCE_PREFIXES:
            if value.startswith(prefix):
                return f"invalid reference path {value!r}: nothing under {prefix!r} can be referenced"
        return None


class ExcludeGlob(RepoRelativePath):
    """A repo-root-relative gitignore-style glob naming paths a footprint never considers."""

    @classmethod
    def _describe_problem(cls, value: str) -> str | None:
        # The built-in excludes are a denylist rather than a default, so a gitignore
        # negation -- which would re-include whatever an earlier pattern excluded -- is
        # refused instead of being honoured.
        if value.startswith(_EXCLUDE_NEGATION_PREFIX):
            return (
                f"invalid exclude glob {value!r}: a leading {_EXCLUDE_NEGATION_PREFIX!r} would "
                "re-include an excluded path, and the excludes are a denylist"
            )
        return _describe_repo_relative_path_problem(value, "exclude glob")


class ReferenceNote(str):
    """One line saying why a referenced artifact belongs to an app and which surface it uses."""

    def __new__(cls, value: str) -> Self:
        if not value.strip():
            raise InvalidManifestValueError("a reference note must not be empty")
        if "\n" in value or "\r" in value:
            raise InvalidManifestValueError(
                f"invalid reference note {value!r}: a note is a single line"
            )
        if len(value) > _MAX_REFERENCE_NOTE_LENGTH:
            raise InvalidManifestValueError(
                f"a reference note must be at most {_MAX_REFERENCE_NOTE_LENGTH} characters, got {len(value)}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )
