from pathlib import Path

from flask import Flask
from flask import current_app
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.config import Config
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.template_catalog import TemplateCatalogStore
from imbue.system_interface.update_staleness import UpdateStalenessTracker

# Key under which the single SystemInterfaceState is stored on ``app.config`` so
# handlers can fetch it via ``get_state()``.
_STATE_CONFIG_KEY = "SYSTEM_INTERFACE_STATE"


class SystemInterfaceStateError(RuntimeError):
    """Raised when the SystemInterfaceState is not attached to a Flask app."""


# The frontend build's output, inside the package: what the shell routes serve
# in production.
DEFAULT_STATIC_DIRECTORY = Path(__file__).parent / "static"


class SystemInterfaceState(MutableModel):
    """Holds the shell's config and collaborators for one system-interface app.

    Built once in ``main.build_production_state`` (or by a test) and stored on the Flask
    app; handlers read it via ``get_state()``.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    config: Config
    # The shell's own collaborators (the inventory, the stores, the activity log).
    shell: ShellState
    # The New Tab page's template catalog: fetched from its URL, cached under the shell's state.
    template_catalog: TemplateCatalogStore
    # Captures the tree HEAD this process started from, so the app shell can
    # say when the served tree has moved under it (see update_staleness.py).
    # A factory (not a shared default): the HEAD read happens per state build,
    # not at import.
    update_staleness: UpdateStalenessTracker = Field(default_factory=UpdateStalenessTracker.capture)
    static_directory: Path = Field(
        default=DEFAULT_STATIC_DIRECTORY,
        description="The bundle directory the shell routes serve from: the package's own static/ unless the "
        "state is built with another (a test serving a shell it wrote)",
    )
    # A preview shell is the real workspace with one app swapped, booted read-only over a
    # copy of the live state: it refuses every verb that would act on a live instance and
    # tells its page so, and it raises no staleness banner of its own.
    is_preview: bool = Field(default=False, description="Whether this shell is a preview of a proposed change")

    def shutdown(self) -> None:
        """Tear down every owned resource. Idempotent."""
        self.shell.broadcaster.shutdown()
        self.shell.stop()


def attach_state(app: Flask, state: SystemInterfaceState) -> None:
    app.config[_STATE_CONFIG_KEY] = state


def get_state() -> SystemInterfaceState:
    """Return the SystemInterfaceState for the current Flask app."""
    state = current_app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the current app")
    return state


def state_of(app: Flask) -> SystemInterfaceState:
    """Return the SystemInterfaceState attached to ``app`` without needing an app context."""
    state = app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the app")
    return state
