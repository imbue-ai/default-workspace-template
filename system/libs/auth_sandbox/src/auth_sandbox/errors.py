"""The auth sandbox library's exceptions."""


class AuthSandboxError(Exception):
    """Base of everything this library raises."""


class MissingMaterialError(AuthSandboxError, FileNotFoundError):
    """A file the connector or the chrome writes before a command runs (a key, an incoming state) is not there."""


class LatchkeyCommandError(AuthSandboxError, RuntimeError):
    """A ``latchkey`` invocation failed; the message carries what it printed, never a key."""


class ValuesFileError(AuthSandboxError, ValueError):
    """The typed-credential values file is not the JSON object the command expects."""


class ImageBuildError(AuthSandboxError, RuntimeError):
    """A step of the sandbox image build failed inside the build sandbox."""
