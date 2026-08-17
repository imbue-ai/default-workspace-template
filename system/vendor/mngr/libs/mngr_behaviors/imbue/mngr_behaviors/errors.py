from imbue.mngr.errors import MngrError


class BehaviorCorpusRootNotFoundError(MngrError, FileNotFoundError):
    """Raised when the behavior corpus root passed to a scan is not an existing directory."""

    ...


class BehaviorValidationFailedError(MngrError):
    """Raised by ``mngr behaviors validate`` when the corpus has language violations (after listing them)."""

    ...


class BehaviorListingIncompleteError(MngrError):
    """Raised by ``mngr behaviors list`` when some units could not be represented as records.

    The representable records are still emitted on stdout first; this error
    (after per-problem stderr diagnostics) makes the incompleteness visible to
    pipelines via the exit code.
    """

    ...


class BehaviorTestsRootNotFoundError(MngrError, FileNotFoundError):
    """Raised by ``mngr behaviors matrix`` when a ``--tests`` path does not exist."""

    ...


class BehaviorWitnessCollectionError(MngrError):
    """Raised by ``mngr behaviors matrix`` when the inner ``pytest --collect-only`` run cannot collect.

    Exit codes 0 (items collected) and 5 (none collected) are both fine; any
    other exit code, a timeout, or unparseable plugin output is a hard failure
    that carries an excerpt of the pytest output.
    """

    ...


class BehaviorDanglingWitnessError(MngrError):
    """Raised by ``mngr behaviors matrix`` when a ``witnesses`` marker does not name a real behavior unit.

    Covers a coordinate matching no corpus unit (dangling) and invalid marker
    usage (no positional coordinate, or a non-string one). The matrix records
    are still emitted on stdout first; this error (after per-marker stderr
    diagnostics) makes the broken links visible via the exit code.
    """

    ...
