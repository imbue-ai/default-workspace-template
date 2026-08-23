class AnalyticsError(Exception):
    """Base exception for the analytics app."""


class AnalyticsConfigError(AnalyticsError, ValueError):
    """Raised when required analytics environment configuration is missing or malformed."""


class LakeAttachError(AnalyticsError, RuntimeError):
    """Raised when a DuckLake catalog or source database cannot be attached."""


class SessionAssemblyError(AnalyticsError, RuntimeError):
    """Raised when a DuckDB session component (extension, secret, log view) cannot be set up."""


class LakeInsertError(AnalyticsError, RuntimeError):
    """Raised when a raw-record batch cannot be committed to a lake."""


class LakeMaintenanceError(AnalyticsError, RuntimeError):
    """Raised when a DuckLake maintenance statement fails."""


class AggregationError(AnalyticsError, RuntimeError):
    """Raised when a gold-table aggregation statement fails."""


class CollectionError(AnalyticsError, RuntimeError):
    """Raised when the collection poll's shared infrastructure fails (never per-workspace)."""


class DeletionError(AnalyticsError, RuntimeError):
    """Raised when the account-deletion path cannot complete."""
