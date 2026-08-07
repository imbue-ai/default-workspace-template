"""opencode's model catalog and its (ON_CHANGE) model resolver.

A near-mirror of pi's, with two upgrades opencode's client-server shape allows: the
pre-turn-1 model comes from the live server (a probe), not None, and a model switch is
a real HTTP call to that server, not a deferred control file.

The catalog is PARSED from opencode's own models.dev cache (the same file the CLI
reads), so it is the full universe of models. Each model's effort options mirror
opencode's OWN variant synthesis rather than the raw models.dev effort values, because
the live selection we read back is opencode's ``variant``:

* an ``effort``-type ``reasoning_options`` entry -> its ``values`` are the variant keys
  (e.g. ``low``/``medium``/``high``);
* a ``budget_tokens`` entry (all Anthropic Claude) -> opencode synthesizes ``high``/
  ``max`` (models.dev lists no values for these);
* a ``toggle``/plain model -> no variant axis (the live variant is always ``""``).

Which models are *offered* (the authenticated subset) is a separate, live concern
handled at picker-open time via ``opencode models`` -- not here. The catalog is the
master list: labels and the effort universe for whatever model an agent is on.
"""

import json
import os
import shutil
import urllib.request
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
from imbue.system_interface.harnesses.model import parse_effort_level
from imbue.system_interface.harnesses.model import to_options
from imbue.system_interface.harnesses.opencode.probe import probe_startup_model
from imbue.system_interface.harnesses.opencode.probe import read_server_port

_ICON: str = (Path(__file__).parent / "icon.svg").read_text()

# opencode's models.dev cache -- byte-identical to the models.dev API response and
# refreshed by opencode itself. Read (not fetched) so this works offline and owns no
# refresh policy. Absent until opencode has run once and written it.
_CACHE_RELATIVE_PATH: tuple[str, ...] = (".cache", "opencode", "models.json")

# The per-agent state file the lifecycle plugin writes: {provider, model, variant}.
# Kept in sync with MODEL_STATE_FILENAME in mngr_opencode's plugin.
_MODEL_STATE_NAME: str = "opencode_model_state.json"

# Per-agent dirs opencode isolates under (OPENCODE_CONFIG_DIR / XDG_DATA_HOME) and the
# file recording its root session id. Kept in sync with mngr_opencode's opencode_config.py
# (_CONFIG_DIR_RELATIVE_PATH, _DATA_HOME_RELATIVE_PATH, ROOT_SESSION_FILENAME).
_CONFIG_DIR_RELPATH: tuple[str, ...] = ("plugin", "opencode", "config")
_DATA_HOME_RELPATH: tuple[str, ...] = ("plugin", "opencode", "data")
_ROOT_SESSION_NAME: str = "opencode_root_session"

# opencode's variant ladder, ascending -- the ONE order a model's efforts emit, so they
# never depend on the order models.dev happened to list them in.
_OPENCODE_EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# variant values meaning "base profile / no specific effort" -> effort None. Everything
# else (a real variant key like ``high``) is the effort.
_BASE_VARIANTS: frozenset[str] = frozenset({"", "default"})

# There is no launch default -- opencode is many-provider/many-auth; the pre-turn-1
# model comes from the probe, the live one from the state file.
OPENCODE_DEFAULT_MODEL_ID: str = ""

_LIST_MODELS_TIMEOUT_SECONDS: float = 15.0
_SWITCH_TIMEOUT_SECONDS: float = 5.0


def _efforts(model: dict[str, Any]) -> tuple[str, ...]:
    """A model's variant keys -- its effort options -- replicating opencode's own synthesis.

    models.dev's ``variants`` is always null, so opencode builds the variant set from
    ``reasoning_options``: an ``effort`` entry contributes its ``values`` verbatim; a
    ``budget_tokens`` entry (all Anthropic Claude) contributes ``high``/``max`` (opencode
    invents these from the thinking budget); a ``toggle``/absent axis contributes nothing
    (the live variant stays ``""``). Ordered by the fixed ladder for a stable dropdown.
    """
    declared: set[str] = set()
    for option in model.get("reasoning_options") or ():
        if not isinstance(option, dict):
            continue
        kind = option.get("type")
        if kind == "effort":
            for value in option.get("values") or ():
                level = parse_effort_level(value)
                if level is not None:
                    declared.add(level)
        elif kind == "budget_tokens":
            declared.update(("high", "max"))
        else:
            # A ``toggle`` (or any other) reasoning axis has no selectable effort levels;
            # opencode leaves the variant as "" for such models.
            continue
    return tuple(level for level in _OPENCODE_EFFORT_LADDER if level in declared)


def build_catalog(cache_path: Path) -> HarnessCatalog:
    """opencode's full catalog from the models.dev cache (``{provider: {models: {id: model}}}``)."""
    catalog = read_json_dict(cache_path)
    entries: list[tuple[str, tuple[str, ...]]] = []
    for provider_id, provider in catalog.items():
        if not isinstance(provider, dict):
            continue
        models_by_id = provider.get("models")
        if not isinstance(models_by_id, dict):
            continue
        for model_id, model in models_by_id.items():
            if isinstance(model, dict):
                entries.append((f"{provider_id}/{model_id}", _efforts(model)))
    return HarnessCatalog(
        options=to_options(tuple(entries)),
        default_model_id=OPENCODE_DEFAULT_MODEL_ID,
        switch_mode=SwitchMode.ON_CHANGE,
        picker_mode=PickerMode.SEARCH,
        icon_svg=_ICON,
    )


def get_catalog() -> HarnessCatalog:
    """opencode's master catalog, or an empty one until its cache exists (invariant: never raise)."""
    cache_path = Path.home().joinpath(*_CACHE_RELATIVE_PATH)
    if not cache_path.is_file():
        logger.warning("opencode models.dev cache not found at {}; opencode's model catalog will be empty", cache_path)
        return HarnessCatalog(
            options=(),
            default_model_id=OPENCODE_DEFAULT_MODEL_ID,
            switch_mode=SwitchMode.ON_CHANGE,
            picker_mode=PickerMode.SEARCH,
            icon_svg=_ICON,
        )
    return build_catalog(cache_path)


def _parse_models_output(output: str) -> tuple[str, ...]:
    """The ``provider/model`` tags from ``opencode models`` -- one per line, no header."""
    tags: list[str] = []
    for line in output.splitlines():
        tag = line.strip()
        if tag and "/" in tag:
            tags.append(tag)
    return tuple(tags)


def _read_root_session_id(state_dir: Path) -> str | None:
    """The agent's root opencode session id, or None when the marker is absent/blank."""
    try:
        session_id = (state_dir / _ROOT_SESSION_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return session_id or None


def _post_session_model(port: int, session_id: str, body: dict[str, Any]) -> bool:
    """POST a model switch to the local opencode server; True on a 2xx, False on any failure."""
    url = f"http://127.0.0.1:{port}/api/session/{session_id}/model"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_SWITCH_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except OSError as e:
        logger.warning("opencode model switch POST {} failed: {}", url, e)
        return False


class OpenCodeModelResolver(HarnessModelResolver):
    """Reads an opencode agent's live model/variant from the lifecycle plugin's state file,
    guesses the pre-turn-1 model from the live server, and switches via that server's API."""

    _state_dir: Path
    # The probe result, computed once: the startup model does not change, and the probe is
    # a timeout-bounded HTTP call that must not run on every model-choice recompute.
    _cached_guess: ModelIdentity | None
    _has_guessed: bool

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "OpenCodeModelResolver":
        self = cls.__new__(cls)
        self._state_dir = agent_info.agent_state_dir
        self._cached_guess = None
        self._has_guessed = False
        return self

    def guess_from_launch(self) -> ModelIdentity | None:
        # opencode's running server resolved a model at startup (from opencode.json / the
        # authed provider default). Probe it once and cache -- read_live takes over once the
        # first assistant message records the live selection. Effort is unknown pre-turn-1
        # (merge_identities fills it from a live read when one appears).
        if not self._has_guessed:
            model_id = probe_startup_model(self._state_dir)
            self._cached_guess = ModelIdentity(model_id=model_id, effort=None, fast=False) if model_id else None
            self._has_guessed = True
        return self._cached_guess

    def read_live(self) -> ModelIdentity | None:
        # The plugin writes {provider, model, variant} on each assistant message. variant is
        # opencode's effort axis; "" and "default" both mean the base profile (no effort).
        data = read_json_dict(self._state_dir / _MODEL_STATE_NAME)
        provider = data.get("provider")
        model = data.get("model")
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            return None
        variant = data.get("variant")
        effort = variant if isinstance(variant, str) and variant not in _BASE_VARIANTS else None
        return ModelIdentity(model_id=f"{provider}/{model}", effort=effort, fast=False)

    def watched_paths(self) -> tuple[Path, ...]:
        return (self._state_dir / _MODEL_STATE_NAME,)

    def list_offered_models(self) -> tuple[str, ...] | None:
        # opencode's offer set is account-gated and dynamic: ``opencode models`` lists exactly
        # the provider/model pairs the agent is authenticated for (reading the agent's own auth
        # via OPENCODE_CONFIG_DIR + XDG_DATA_HOME). Run per picker-open so a fresh login shows
        # up. On any failure, return None (offer the whole catalog) rather than an empty picker.
        executable = shutil.which("opencode")
        if executable is None:
            return None
        config_dir = self._state_dir.joinpath(*_CONFIG_DIR_RELPATH)
        data_home = self._state_dir.joinpath(*_DATA_HOME_RELPATH)
        env = {**os.environ, "OPENCODE_CONFIG_DIR": str(config_dir), "XDG_DATA_HOME": str(data_home)}
        try:
            finished = run_local_command_modern_version(
                [executable, "models"],
                is_checked=False,
                timeout=_LIST_MODELS_TIMEOUT_SECONDS,
                env=env,
                name="opencode models",
            )
        except ProcessSetupError as e:
            logger.warning("opencode models could not start for {}: {}", self._state_dir, e)
            return None
        if finished.is_timed_out or finished.returncode != 0:
            logger.warning(
                "opencode models failed for {} (timed_out={}, returncode={})",
                self._state_dir,
                finished.is_timed_out,
                finished.returncode,
            )
            return None
        return _parse_models_output(finished.stdout)

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # opencode is client-server: a switch is a session-level HTTP call to the live server
        # (POST /api/session/{id}/model, which sets model AND variant together), applied to the
        # next turn. ON_CHANGE: the chip reconciles from the state file once the next assistant
        # message records the new selection. ``send`` (a pane command) is unused here.
        if ModelAxis.MODEL not in axes and ModelAxis.EFFORT not in axes:
            return SwitchResult(ok=True)
        port = read_server_port(self._state_dir)
        session_id = _read_root_session_id(self._state_dir)
        if port is None or session_id is None:
            return SwitchResult(ok=False, detail="opencode server is not running")
        provider_id, _, model_name = identity.model_id.partition("/")
        if not provider_id or not model_name:
            return SwitchResult(ok=False, detail=f"Malformed opencode model id '{identity.model_id}'")
        variant = identity.effort if identity.effort is not None else ""
        body = {"model": {"providerID": provider_id, "id": model_name, "variant": variant}}
        if not _post_session_model(port, session_id, body):
            return SwitchResult(ok=False, detail="Failed to switch the opencode model")
        return SwitchResult(ok=True)
