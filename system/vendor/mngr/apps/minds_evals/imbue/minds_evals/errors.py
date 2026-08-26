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


class AgentKwargError(MindsEvalsError, ValueError):
    """Raised when an `--ak key=value` agent kwarg cannot be read as the type it selects.

    Raised from the driver's constructor, so a run stops before any box boots rather than after a
    trial has burned a workspace on a setting that was never applied."""

    ...
