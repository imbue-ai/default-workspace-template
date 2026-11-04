"""The auth sandbox library's exceptions."""


class AuthSandboxError(Exception):
    """Base of everything this library raises."""


class MissingMaterialError(AuthSandboxError, FileNotFoundError):
    """A material a command needs is missing or unusable: a file the chrome or an earlier command writes before it
    runs (the session's key or settings), or the ``latchkey`` binary itself."""


class LatchkeyCommandError(AuthSandboxError, RuntimeError):
    """A ``latchkey`` invocation failed; the message carries what it printed, never a key."""


class ValuesFileError(AuthSandboxError, ValueError):
    """The typed-credential values file is not the JSON object the command expects."""


class SessionFileError(AuthSandboxError, ValueError):
    """The session file, or the settings ``auth-sandbox-prepare`` kept from it, is not what the commands expect."""


class ClaimFileError(AuthSandboxError, ValueError):
    """The connector's claim file is not the JSON object the claim watcher expects."""


class ImageBuildError(AuthSandboxError, RuntimeError):
    """A step of the sandbox image build failed inside the build sandbox."""
