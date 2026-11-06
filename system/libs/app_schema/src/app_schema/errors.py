class AppSchemaError(Exception):
    """Base of every error the library raises."""


class InvalidRequestBodyError(AppSchemaError, ValueError):
    """A request body that is not a JSON object, or not the shape its route's model requires (a 400)."""


class RouteModelsMismatchError(AppSchemaError, RuntimeError):
    """A route parsed or answered a model its app's route table does not name for it: the document would lie."""


class RouteTableConflictError(AppSchemaError, ValueError):
    """Two route tables merged into one app's table name the same route."""
