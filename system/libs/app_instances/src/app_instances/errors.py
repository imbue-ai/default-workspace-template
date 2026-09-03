class AppInstancesError(Exception):
    """Base error for everything in the app_instances library."""


class InvalidInstanceValueError(AppInstancesError, ValueError):
    """An instance key, URL, path, title, or key prefix does not satisfy its rule."""


class UnknownActionError(AppInstancesError):
    """A create named an action the app does not declare (answered 400)."""


class InvalidParamsError(AppInstancesError):
    """A create's params are not what its action accepts (answered 400)."""


class UnknownInstanceError(AppInstancesError):
    """No instance has the given key (answered 404)."""


class NotRenameableError(AppInstancesError):
    """The instance does not accept a rename (answered 400)."""


class LocationNotTrackedError(AppInstancesError):
    """The app does not record where an instance's page is (answered 400)."""


class InstanceConflictError(AppInstancesError):
    """The app cannot do this now: a create it refuses, or a title another instance already has (answered 409)."""


class NotReadyError(AppInstancesError):
    """The app is still initialising and has no answer yet (answered 503)."""


class InstanceStoreError(AppInstancesError):
    """The JSON instance store cannot be read or written."""


class SidecarError(AppInstancesError):
    """The sidecar launcher cannot start: a manifest that does not fit, a port it cannot bind, or a failed registration."""
