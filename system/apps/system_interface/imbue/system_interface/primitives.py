from imbue.imbue_common.primitives import NonEmptyStr


class ServiceName(NonEmptyStr):
    """Name of a service registered under ``data/.state/apps.toml`` (e.g. 'web', 'terminal')."""

    ...
