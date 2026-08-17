class MindsEvalsError(Exception):
    """Base exception for all minds_evals errors."""

    ...


class EvalConfigError(MindsEvalsError, ValueError):
    """Raised when an eval config file is missing, malformed, or fails validation."""

    ...


class MngrSourceError(MindsEvalsError, RuntimeError):
    """Raised when the mngr source ref cannot be resolved or fetched."""

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
