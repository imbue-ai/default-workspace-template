class AppManifestError(Exception):
    """Base error for everything in the app_manifest library."""


class InvalidManifestValueError(AppManifestError, ValueError):
    """A manifest or registry value does not satisfy its rule."""


class ManifestLoadError(AppManifestError):
    """An app.toml file cannot be read, parsed, or validated."""


class RegistryReadError(AppManifestError):
    """The registry file cannot be read or parsed at all (a bad row is skipped instead)."""
