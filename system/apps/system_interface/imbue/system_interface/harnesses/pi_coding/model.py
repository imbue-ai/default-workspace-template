"""pi's model catalog and its (ON_CHANGE) model resolver.

claude and codex declare a handful of models by hand. pi exposes over a thousand
across dozens of providers, each with its own reasoning ("thinking") levels no human
would maintain -- so pi's catalog is PARSED, in the same container, from the same
provider data files pi itself reads. The product is an ordinary
:class:`HarnessCatalog`; nothing downstream can tell a parsed catalog from a
hand-written one, except that pi's uses ``PickerMode.SEARCH`` (the option set is huge
and account-gated, so the picker is a search box, not a list).

The catalog is the master list: it supplies each model's effort levels (verbatim
strings from pi's data) and the label for whatever model an agent is on. Which models
are *offered* to a user is a separate, live concern (``pi --list-models`` for the
authed subset), handled at picker-open time, not here.

The live selection is read by the shared reader
(:func:`~imbue.system_interface.harnesses.model.read_model_identity`) from the uniform
``minds_model_state.json`` the pi lifecycle extension writes at the agent state-dir root,
refreshed at session start (before the first turn), on every ``/model`` or thinking-level
change, and on resume. There is no launch default -- pi is many-provider/many-auth -- so
the bar shows logo-only until the extension records a model. This resolver only owns the
WRITE (switch) side and the auth-gated picker offer set (:meth:`list_offered_models`).
"""

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.mngr.utils.file_utils import read_json_dict
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.model import to_options

# The control file the resolver appends a switch intent to; the pi extension watches it
# and applies via pi.setModel / pi.setThinkingLevel (kept in sync with the extension).
_CONTROL_NAME: str = "pi_control.jsonl"

# The lifecycle extension writes minds_model_state.json at the agent state-dir root; the
# registry wires this as the harness's model_state_relative_path (the shared reader reads there).
PI_STATE_RELATIVE_PATH: Path = Path(".")

# pi's thinking ladder, in pi's own order (pi-ai's getSupportedThinkingLevels iterates
# this order). This is pi's ordering, not a curated effort set -- which levels a given
# model actually offers comes from that model's own data below, verbatim as strings.
_PI_THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# The per-agent pi config dir (== PI_CODING_AGENT_DIR), where the agent's auth lives.
# Kept in sync with _PI_CONFIG_DIR_RELPATH in mngr_pi_coding's plugin.py.
_PI_CONFIG_DIR_RELPATH: str = "plugin/pi_coding"
# How long to wait for `pi --list-models` before falling back to the whole catalog.
_LIST_MODELS_TIMEOUT_SECONDS: float = 15.0


def _parse_list_models(output: str) -> tuple[str, ...]:
    """The ``provider/model`` tags from ``pi --list-models`` table output.

    The output is a whitespace-column table led by a ``provider  model  ...`` header;
    each data row's first two columns are the provider and model. We skip everything up
    to and including the header, then take the first two tokens of each row. When there
    is no header at all (pi prints "No models available. Use /login ..." when unauthed),
    the result is empty -- meaning the user can offer nothing, which is correct.
    """
    tags: list[str] = []
    header_seen = False
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if not header_seen:
            if parts[0] == "provider" and parts[1] == "model":
                header_seen = True
            continue
        tags.append(f"{parts[0]}/{parts[1]}")
    return tuple(tags)


def _supported_thinking_levels(model: dict[str, Any]) -> tuple[str, ...]:
    """pi's ``getSupportedThinkingLevels``, ported from pi-ai.

    The ``reasoning`` gate comes FIRST and short-circuits: a non-reasoning model
    supports only ``off``, whatever its ``thinkingLevelMap`` says. Past that gate the
    map is a SPARSE OVERRIDE, not the level set: a null mapping disables that level,
    ``xhigh``/``max`` are offered only when explicitly mapped, and every other level is
    on unless nulled.
    """
    if not model.get("reasoning"):
        return ("off",)
    level_map = model.get("thinkingLevelMap") or {}
    supported: list[str] = []
    for level in _PI_THINKING_LEVELS:
        if level in level_map and level_map[level] is None:
            continue
        if level in ("xhigh", "max") and level not in level_map:
            continue
        supported.append(level)
    return tuple(supported)


def find_provider_data_dir(pi_executable: Path) -> Path | None:
    """pi's bundled provider data dir, resolved from the ``pi`` binary itself.

    ``pi`` is a symlink to ``dist/cli.js`` inside the globally installed
    ``@earendil-works/pi-coding-agent``, whose npm prefix differs per image. Walking up
    from the resolved link finds the data dir without hardcoding a prefix or shelling
    out to node.
    """
    current = pi_executable.resolve()
    while current != current.parent:
        candidate = current / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "providers" / "data"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return None


def build_catalog(data_dir: Path) -> HarnessCatalog:
    """pi's full catalog from ``<pi-ai>/dist/providers/data/*.json``.

    One file per provider, each shaped ``{api_name: {model_id: model}}``. The provider
    id is the filename stem (equal to the ``provider`` field on every model). Each
    model's efforts are its supported thinking levels, verbatim strings.
    """
    entries: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(data_dir.glob("*.json")):
        provider_id = path.stem
        for models_by_id in read_json_dict(path).values():
            if not isinstance(models_by_id, dict):
                continue
            for model_id, model in models_by_id.items():
                if isinstance(model, dict):
                    entries.append((f"{provider_id}/{model_id}", _supported_thinking_levels(model)))
    return HarnessCatalog(
        options=to_options(tuple(entries)),
        switch_mode=SwitchMode.ON_CHANGE,
        picker_mode=PickerMode.SEARCH,
        powered_by_label="Pi Coding",
        # pi is not ready for atomic shoulder-tap; keep the restart-based flush.
        native_atomic_shoulder_tap_possible=False,
    )


def get_catalog() -> HarnessCatalog:
    """pi's master catalog, or an empty one when pi's data is absent (invariant: never raise)."""
    executable = shutil.which("pi")
    data_dir = find_provider_data_dir(Path(executable)) if executable else None
    if data_dir is None:
        logger.warning("pi provider data not found; pi's model catalog will be empty")
        return HarnessCatalog(
            options=(),
            switch_mode=SwitchMode.ON_CHANGE,
            picker_mode=PickerMode.SEARCH,
            powered_by_label="Pi Coding",
            # pi is not ready for atomic shoulder-tap; keep the restart-based flush.
            native_atomic_shoulder_tap_possible=False,
        )
    return build_catalog(data_dir)


class PiModelResolver(HarnessModelResolver):
    """Applies a pi agent's switch by writing a control file the extension consumes, and
    reports its auth-gated picker offer set (the live read is shared)."""

    _state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "PiModelResolver":
        self = cls.__new__(cls)
        self._state_dir = agent_info.agent_state_dir
        return self

    def list_offered_models(self) -> tuple[str, ...] | None:
        # pi's offer set is account-gated and dynamic: exactly the provider/model pairs the
        # user is authenticated for, which `pi --list-models` reports (reading the agent's own
        # auth via PI_CODING_AGENT_DIR). Run per picker-open so a fresh /login shows up. The
        # full catalog stays the master list -- these ids are matched back to it for labels
        # and thinking levels. On any failure, return None (offer the whole catalog) rather
        # than an empty picker.
        executable = shutil.which("pi")
        if executable is None:
            return None
        pi_config_dir = self._state_dir / _PI_CONFIG_DIR_RELPATH
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(pi_config_dir)}
        try:
            finished = run_local_command_modern_version(
                [executable, "--list-models"],
                is_checked=False,
                timeout=_LIST_MODELS_TIMEOUT_SECONDS,
                env=env,
                name="pi --list-models",
            )
        except ProcessSetupError as e:
            logger.warning("pi --list-models could not start for {}: {}", pi_config_dir, e)
            return None
        if finished.is_timed_out or finished.returncode != 0:
            logger.warning(
                "pi --list-models failed for {} (timed_out={}, returncode={})",
                pi_config_dir,
                finished.is_timed_out,
                finished.returncode,
            )
            return None
        return _parse_list_models(finished.stdout)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # pi's inbox delivers user messages, not slash commands, so a switch cannot go
        # through ``send``. Instead the resolver appends the intent to a control file the
        # lifecycle extension watches and applies via pi.setModel / pi.setThinkingLevel.
        # ON_CHANGE: the chip reconciles from the state file once the extension applies it.
        if ModelAxis.MODEL not in axes and ModelAxis.EFFORT not in axes:
            return SwitchResult(ok=True)
        intent = {"model_id": identity.model_id, "thinking_level": identity.effort}
        control_path = self._state_dir / _CONTROL_NAME
        try:
            with control_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(intent) + "\n")
        except OSError as e:
            logger.warning("pi switch: failed to write control file {}: {}", control_path, e)
            return SwitchResult(ok=False, detail="Failed to record the model switch")
        return SwitchResult(ok=True)
