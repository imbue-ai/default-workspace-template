from functools import cached_property
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

from imbue.system_interface.agent_events import AgentEventsMode


class DuplicateStaticBasenameError(ValueError):
    pass


class Config(BaseSettings):
    model_config = {"frozen": False}

    system_interface_javascript_plugins: list[str] | None = None
    system_interface_static_paths: list[str] | None = None
    system_interface_host: str = "127.0.0.1"
    system_interface_port: int = 8000
    # Where workspace layouts are read and written, overriding the usual
    # MNGR_AGENT_ID-derived path. The system-interface live-editing preview points
    # this at a throwaway copy of the live layout so the preview renders the
    # user's real tabs while its own autosaves land in the copy. Config-scoped
    # rather than read from the ambient process env so that several servers
    # sharing one process (the test setup) cannot clobber each other's layouts.
    system_interface_layout_dir: Path | None = None
    # How this instance gets agent lifecycle events. The default (OBSERVE) runs
    # ``mngr observe``, which needs the single-writer observe lock for the mngr
    # host dir. A second system interface on the same host -- the live-editing
    # preview, or the reveal script's pre-flight boot -- must be launched with
    # FOLLOW so it reads the running observer's event stream instead of fighting
    # it for the lock (which would leave its agent view frozen from boot).
    system_interface_agent_events_mode: AgentEventsMode = AgentEventsMode.OBSERVE
    # Service names that resolve back to *this* instance, so proxying them would
    # nest this instance inside itself. The service dispatcher serves a short
    # explanation for these instead of forwarding. The live-editing preview sets
    # it to its own two service names: the user's seeded layout legitimately
    # contains the preview tab (it stays open across the whole editing loop), and
    # rendering that tab would proxy back to the wrapper framing it -- infinitely
    # nested iframes, each loading a full system interface. Empty by default: the
    # workspace's own system interface is not reachable as a `/service/` name at
    # all, so it has nothing to exclude.
    system_interface_self_referential_services: list[str] | None = None

    @field_validator(
        "system_interface_javascript_plugins",
        "system_interface_static_paths",
        "system_interface_self_referential_services",
        mode="before",
    )
    @classmethod
    def split_comma_separated(cls, value: object) -> list[str] | None:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return None

    @cached_property
    def self_referential_service_names(self) -> frozenset[str]:
        """The self-referential service names, as a set for per-request lookup."""
        return frozenset(self.system_interface_self_referential_services or ())

    @cached_property
    def javascript_plugin_basenames(self) -> list[str]:
        if not self.system_interface_javascript_plugins:
            return []
        return [Path(plugin_path).name for plugin_path in self.system_interface_javascript_plugins]

    @cached_property
    def static_file_basename_to_path(self) -> dict[str, str]:
        all_paths = [
            *(self.system_interface_javascript_plugins or []),
            *(self.system_interface_static_paths or []),
        ]
        if not all_paths:
            return {}
        result: dict[str, str] = {}
        for file_path in all_paths:
            basename = Path(file_path).name
            if basename in result:
                raise DuplicateStaticBasenameError(
                    f"Duplicate basename '{basename}': '{result[basename]}' and '{file_path}'"
                )
            result[basename] = file_path
        return result


def load_config() -> Config:
    return Config()
