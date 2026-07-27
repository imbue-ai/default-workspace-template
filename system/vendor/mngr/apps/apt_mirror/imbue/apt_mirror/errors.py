class AptMirrorError(Exception):
    """Base exception for apt mirror failures."""


class AptMirrorNotConfiguredError(AptMirrorError, RuntimeError):
    """Raised when the apt mirror R2 env configuration is absent."""

    def __init__(self, missing_env_var: str) -> None:
        super().__init__(
            f"apt mirror is not configured: {missing_env_var} is unset "
            "(see .minds/template/apt-mirror.sh for the Vault-backed schema)"
        )


class AptMirrorObjectNotFoundError(AptMirrorError, LookupError):
    """Raised when a requested object exists neither in the cache nor upstream."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Object not found in mirror or upstream: {path}")


class AptMirrorUnsafePathError(AptMirrorError, ValueError):
    """Raised when a path could escape the archive tree."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Unsafe archive path: {path!r}")


class AptMirrorInvalidTimestampError(AptMirrorError, ValueError):
    """Raised when a snapshot timestamp does not match the snapshot.debian.org format."""

    def __init__(self, timestamp: str) -> None:
        super().__init__(f"Invalid snapshot timestamp {timestamp!r} (expected YYYYMMDDTHHMMSSZ)")


class AptMirrorUpstreamError(AptMirrorError, RuntimeError):
    """Raised when an upstream archive answers with an unexpected HTTP status."""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"Upstream {url} returned {status_code}")


class AptMirrorTransientUpstreamError(AptMirrorUpstreamError):
    """Raised for throttling and server errors (429/5xx) that are worth retrying."""


class AptMirrorChecksumMismatchError(AptMirrorError, RuntimeError):
    """Raised when a fetched index file does not match the Release-declared sha256."""

    def __init__(self, path: str, expected_sha256: str, actual_sha256: str) -> None:
        super().__init__(f"Checksum mismatch for {path}: expected {expected_sha256}, got {actual_sha256}")


class AptMirrorNotCutError(AptMirrorError, LookupError):
    """Raised when warm or verify is requested for a timestamp that has not been cut."""

    def __init__(self, timestamp: str, missing_key: str) -> None:
        super().__init__(f"Timestamp {timestamp} has not been cut (missing {missing_key}); run cut first")


class AptMirrorPackageListError(AptMirrorError, ValueError):
    """Raised when a package list file is missing or unreadable."""


class AptMirrorTimestampFileError(AptMirrorError, ValueError):
    """Raised when the current-timestamp file is missing or unreadable."""
