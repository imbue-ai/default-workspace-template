class RoutinesError(Exception):
    """Base exception for the routines library."""


class InvalidRoutineIdError(RoutinesError, ValueError):
    """A string that is not a routine id."""


class InvalidDurationError(RoutinesError, ValueError):
    """A string that is not a duration, or a calendar cadence used where a fixed length is needed."""


class InvalidTimeOfDayError(RoutinesError, ValueError):
    """A string that is not a time of day."""


class DefinitionValidationError(RoutinesError, ValueError):
    """A definition file that does not say what a routine is."""


class RoutineNotFoundError(RoutinesError, KeyError):
    """No definition by that id."""

    def __init__(self, routine_id: str) -> None:
        self.routine_id = routine_id
        super().__init__(f"There is no routine called {routine_id!r}.")

    def __str__(self) -> str:
        return f"There is no routine called {self.routine_id!r}."


class RoutineExistsError(RoutinesError, ValueError):
    """A definition by that id already exists."""


class RunInFlightError(RoutinesError, RuntimeError):
    """A run of the routine is already going."""


class DefinitionCommitError(RoutinesError, RuntimeError):
    """A definition was written but could not be committed."""


class StateRemovalError(RoutinesError, RuntimeError):
    """A routine's state directory could not be deleted whole."""


class ChatAppError(RoutinesError, RuntimeError):
    """The chat app could not be reached, or refused."""


class ChatCreateError(RoutinesError, RuntimeError):
    """The routine's chat could not be created."""


class RunNotFoundError(RoutinesError, KeyError):
    """No run by that id."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"There is no run called {run_id!r}.")

    def __str__(self) -> str:
        return f"There is no run called {self.run_id!r}."
