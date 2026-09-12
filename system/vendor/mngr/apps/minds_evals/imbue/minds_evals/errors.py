class MindsEvalsError(Exception):
    """Base exception for all minds_evals errors."""

    ...


class EvalConfigError(MindsEvalsError, ValueError):
    """Raised when an eval config file is missing, malformed, or fails validation."""

    ...


class GitSourceError(MindsEvalsError, RuntimeError):
    """Raised when a pinned git source ref (mngr or the workspace template) cannot be resolved or
    fetched."""

    ...


class BoxCommandError(MindsEvalsError, RuntimeError):
    """Raised when a command executed inside the box environment fails."""

    ...


class WorkspaceCreateError(MindsEvalsError, RuntimeError):
    """Raised when the Minds API fails to create a workspace."""

    ...


class InstructionParseError(MindsEvalsError, ValueError):
    """Raised when the task instruction does not carry a parseable case config block."""

    ...


class JobReadError(MindsEvalsError, ValueError):
    """Raised when a harbor job directory cannot be read as a finished run at all.

    Distinct from a run that failed: a job whose artifacts cannot be parsed has not been judged, and
    reporting it as a failing run would put a harness fault on the eval's record."""

    ...


class TrajectoryDocumentError(MindsEvalsError, ValueError):
    """Raised when a trajectory document captured from the workspace is not valid ATIF."""

    ...


class CleanupScopeError(MindsEvalsError, ValueError):
    """Raised when an environment-cleanup request names a scope that could reach environments the
    caller did not create."""

    ...


class ModalNameBudgetError(MindsEvalsError, ValueError):
    """Raised when a derived Modal name would exceed the length Modal, or mngr on its way there,
    would silently truncate it to -- which would make the name this app records different from the
    one that gets created."""

    ...


class ModalAdminError(MindsEvalsError, RuntimeError):
    """Raised when the Modal SDK cannot answer an environment listing or deletion the cleanup needs."""

    ...


class CapturedFileError(MindsEvalsError, ValueError):
    """Raised when a CapturedFile claims to be both captured and failed, or neither."""

    ...


class AgentKwargError(MindsEvalsError, ValueError):
    """Raised when an `--ak key=value` agent kwarg cannot be read as the type it selects.

    Raised from the driver's constructor, so a run stops before any box boots rather than after a
    trial has burned a workspace on a setting that was never applied."""

    ...


class CiMatrixError(MindsEvalsError, ValueError):
    """The scheduled run's inputs cannot be turned into a matrix: an unreadable or invalid harness
    configs file, a selection naming a config it does not hold, or a pairs or markers file that
    is not shaped as the resolve job writes it."""

    ...
