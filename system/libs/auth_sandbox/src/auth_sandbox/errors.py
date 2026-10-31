"""The auth sandbox library's exceptions."""


class AuthSandboxError(Exception):
    """Base of everything this library raises."""


class MissingMaterialError(AuthSandboxError, FileNotFoundError):
    """A material a command needs is missing or unusable: a file the connector or the chrome writes before it runs
    (a key, an incoming state), or the ``latchkey`` binary itself."""


class LatchkeyCommandError(AuthSandboxError, RuntimeError):
    """A ``latchkey`` invocation failed; the message carries what it printed, never a key."""


class ValuesFileError(AuthSandboxError, ValueError):
    """The typed-credential values file is not the JSON object the command expects."""


class ImageBuildError(AuthSandboxError, RuntimeError):
    """A step of the sandbox image build failed inside the build sandbox."""
