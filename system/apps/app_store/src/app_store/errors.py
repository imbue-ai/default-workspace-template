class AppStoreError(Exception):
    """Base error for the App Store app."""


class CatalogError(AppStoreError, ValueError):
    """The shipped app catalog does not validate."""


class CatalogEntryError(AppStoreError, ValueError):
    """One catalog entry (or one field of it) breaks a rule; pydantic reports it as that field's validation error."""


class InstallError(AppStoreError):
    """An install, repair, update or uninstall step failed; the message says which and why, in plain words."""


class InstallRefusedError(AppStoreError):
    """An install, update or uninstall was asked for that the store will not start (unknown app, won't fit, busy,
    ...)."""


class AddAppRefusedError(AppStoreError):
    """An "Add app" request breaks one of the rules for added apps; the message says which, in plain words."""


class PackageLookupError(AppStoreError):
    """A package could not be found on, or looked up from, its registry."""


class StoreStateError(AppStoreError, OSError):
    """The store's install records cannot be read or written."""


class PortError(AppStoreError):
    """No free port could be found for an app."""
