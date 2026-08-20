"""Claude Code's model catalog and its model resolver.

The one place Claude's model bar behavior lives: the static catalog (the models
``claude --model`` accepts, their labels, effort set, and fast support), and the
:class:`ClaudeModelResolver` that applies a change by sending Claude Code the
``/model`` / ``/effort`` / ``/fast`` slash commands. The live READ is
harness-neutral: Claude's statusline script writes the uniform
``model_state.json`` at the agent state-dir root, which the shared reader
parses; this resolver never reads it.

Claude Code exposes no stable programmatic model list, so the catalog is
maintained by hand to match the aliases ``claude --model`` accepts. Read out of the
pinned 2.1.227 binary, the full alias set is ``default``, ``fable``, ``fable[1m]``,
``mythos``, ``opus``, ``opus[1m]``, ``opusplan``, ``opusplan[1m]``, ``sonnet``,
``sonnet[1m]``, ``haiku``; the catalog offers four of them and hides a fifth, leaving
``default``, ``opusplan`` and the redundant bare variants out on purpose.

Every offered model uses the ``[1m]`` variant to keep the 1M-token context window the
workspace provisions. In Claude Code ``[1m]`` is an explicit opt-in ("append [1m] to the
model name for 1M"), so the bare alias hands back a smaller window than the workspace
paid for. Both forms are accepted and both report the same display name, which is why
the difference is invisible at the ``/model`` prompt -- ``/model fable`` and ``/model
fable[1m]`` each answer "Set model to Fable 5". Do not take an API-level claim that a
model's default context is already 1M as licence to drop the suffix: that is a property
of the model, and this is a property of the harness. Haiku has no ``[1m]`` variant.

The switch alias and the reported id are different strings, and the suffix shows up in
both: an agent launched as ``opus[1m]`` reports ``claude-opus-5[1m]``. Fast mode is an
Opus-only capability (2.1.227 scopes it to "Opus 5/4.8") -- notably NOT a property of
the most capable model, so do not infer it from rank.

The picker offers exactly four models -- Fable 5, Opus 5, Sonnet 5, Haiku 4.5. Every
other option is declared with ``in_picker=False``: matchable if a live read reports it,
never offered. That set is defined by what the four do NOT cover, so an agent sitting on
a model the picker cannot reach still shows a name instead of falling through to the
shrug case. Two ways in: an approved org launching Mythos, and a user typing ``/model
opus-4-8`` straight into the underlying Claude Code session, which the picker neither
offers nor prevents. The ``ultra`` effort (ultracode) is declared-but-hidden the same way.

Each option's ``harness_reported_model_id`` is the suffix-free API id
(``claude-opus-5``), matched against a live read. An option launched with the ``[1m]``
suffix reports it too (``claude-opus-5[1m]``), which reaches the same option through
:func:`match_option`'s prefix pass rather than by an exact key hit. That prefix pass
walks the options in catalog order and takes the first key the reported id starts with,
so the hidden keys are chosen never to prefix one another -- ``claude-opus-4-5`` is not
a prefix of ``claude-opus-4-6``, and the bare family keys (``claude-opus-4``) that WOULD
swallow their dated siblings are deliberately absent.

Hidden options are not decoration: they make the catalog complete against what the
harness can report, and each says what its model actually does. ``supports_fast`` follows
the binary (only Opus 4.8 among them has fast). Efforts stay permissive -- per-model
effort support is not stated plainly anywhere and the matching gate gives a shrug on a
level the option does not declare, so guessing narrow trades a real risk for nothing.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_claude.claude_config import get_agent_hook_settings_path
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult

# Every Claude model offers the same efforts: low..max shown, ultra (ultracode)
# declared-but-hidden. Effort levels are plain strings, as the catalog carries them.
_CLAUDE_EFFORTS: tuple[EffortChoice, ...] = (
    EffortChoice(level="low"),
    EffortChoice(level="medium"),
    EffortChoice(level="high"),
    EffortChoice(level="xhigh"),
    EffortChoice(level="max"),
    EffortChoice(level="ultra", in_picker=False),
)

# The four models the picker offers, in the order claude 2.1.227's own /model picker
# ranks them. Each takes the [1m] alias where one exists, because in Claude Code that
# suffix is an explicit opt-in for the 1M window the workspace provisions -- the bare
# alias is accepted and reports the same display name, so the smaller window it hands
# back is invisible at the prompt. Haiku has no [1m] variant.
_OFFERED_MODELS: tuple[ModelOption, ...] = (
    ModelOption(
        id="fable[1m]",
        label="Fable 5",
        efforts=_CLAUDE_EFFORTS,
        supports_fast=False,
        harness_reported_model_id="claude-fable-5",
    ),
    ModelOption(
        id="opus[1m]",
        label="Opus 5",
        efforts=_CLAUDE_EFFORTS,
        supports_fast=True,
        harness_reported_model_id="claude-opus-5",
    ),
    ModelOption(
        id="sonnet[1m]",
        label="Sonnet 5",
        efforts=_CLAUDE_EFFORTS,
        supports_fast=False,
        harness_reported_model_id="claude-sonnet-5",
    ),
    ModelOption(
        id="haiku",
        label="Haiku 4.5",
        efforts=_CLAUDE_EFFORTS,
        supports_fast=False,
        harness_reported_model_id="claude-haiku-4-5",
    ),
)

# Everything the four offered models do NOT match, so the catalog is complete against
# what the harness can report: Mythos (approved orgs only) and every previous-generation
# model 2.1.227 still carries. These are not offered, but they are not decoration either
# -- they exist so the catalog describes the whole model surface, and each one should say
# what its model actually does.
#
# supports_fast therefore follows the binary rather than being set permissively. 2.1.227
# scopes fast to "Opus 5/4.8", so Opus 4.8 declares it and nothing else does -- 4.7 and
# 4.6 had fast removed, which is also why their legacy claude-opus-4-*-fast ids are dead
# and cannot arrive with fast on. Note the field gates MATCHING, not just rendering: a
# live read of a model with fast on shrugs unless that model declares it. Opus 4.8 with
# fast on is a reachable state, hence the exception; the rest are not.
#
# Efforts do NOT follow the same rule and stay permissive. Per-model effort support is
# not something the binary states plainly (Opus 4.5 takes low/medium/high, Sonnet 4.5
# rejects the axis outright), and the same matching gate applies -- a level an option
# does not declare is a shrug. Guessing narrow there trades a real risk for no benefit,
# since a hidden option's effort set is never shown in a picker.
#
# ORDER IS LOAD-BEARING. match_option's prefix pass takes the FIRST key the reported id
# starts with, so a general key placed before a specific one swallows it -- claude-opus-4
# ahead of claude-opus-4-8 would label every dated Opus 4.x as "Opus 4". Specific keys
# come first and the bare family catch-alls last; the ordering is pinned by a test. The
# catch-alls are what absorb the dated Opus 4 / Sonnet 4 ids (claude-opus-4-20250514),
# whose alias form (claude-opus-4-0) is not a prefix of them.
#
_HIDDEN_MODELS: tuple[ModelOption, ...] = tuple(
    ModelOption(
        id=model_id,
        label=label,
        efforts=_CLAUDE_EFFORTS,
        supports_fast=supports_fast,
        in_picker=False,
        harness_reported_model_id=model_id,
    )
    for model_id, label, supports_fast in (
        ("claude-mythos-5", "Mythos 5", False),
        ("claude-mythos-preview", "Mythos Preview", False),
        # The one hidden model that really has fast, per 2.1.227's "Opus 5/4.8".
        ("claude-opus-4-8", "Opus 4.8", True),
        ("claude-opus-4-7", "Opus 4.7", False),
        ("claude-opus-4-6", "Opus 4.6", False),
        ("claude-opus-4-5", "Opus 4.5", False),
        ("claude-opus-4-1", "Opus 4.1", False),
        ("claude-sonnet-4-6", "Sonnet 4.6", False),
        ("claude-sonnet-4-5", "Sonnet 4.5", False),
        # The binary also carries a dotted spelling; without it the catch-all below would
        # claim it and call it "Sonnet 4".
        ("claude-sonnet-4.6", "Sonnet 4.6", False),
        # Family catch-alls, last on purpose (see ORDER above).
        ("claude-opus-4", "Opus 4", False),
        ("claude-sonnet-4", "Sonnet 4", False),
        ("claude-haiku-4", "Haiku 4", False),
    )
)

# FOR FUTURE MAINTAINERS (human or AI): this object is the ONLY source of truth for which
# models exist, what each is called, which alias switches to it, and which can go fast.
# Never hand-write that table into a doc, changelog, PR body or review comment -- it goes
# stale the first time anyone edits this tuple, and the stale copy is what people then
# trust. Read it out of here instead; every field is available at runtime:
#
#     for option in CLAUDE_CATALOG.options:
#         key = option.harness_reported_model_id or option.id
#         print(option.label, option.id, key, option.in_picker, option.supports_fast)
#
# test_every_binary_model_id_resolves pins the id list this has to cover, so regenerate
# that list against the pinned binary whenever CLAUDE_CODE_VERSION moves.
CLAUDE_CATALOG: HarnessCatalog = HarnessCatalog(
    options=_OFFERED_MODELS + _HIDDEN_MODELS,
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    # No credit for claude: the harness declares an empty string, so nothing renders.
    powered_by_text="",
    # The "Shoulder tap" flushes claude's queue natively (a meta+q -> chat:cancel chord
    # delivered via mngr) instead of the SIGKILL-restart base path. See harnesses/claude/tap.py.
    native_atomic_shoulder_tap_possible=True,
)

# The statusline writes model_state.json at the agent state-dir root; the registry
# wires this as the harness's model_state_relative_path (the shared reader reads there).
CLAUDE_STATE_RELATIVE_PATH: Path = Path(".")


class FastModeSettingsError(RuntimeError):
    """Raised when an agent's Claude settings file cannot be updated safely."""


def _get_agent_fast_mode_write_path(claude_config_dir: Path, agent_state_dir: Path) -> Path:
    """The per-agent settings file a fast-mode change must be recorded in.

    mngr keeps each agent's launch settings under its state dir and re-applies them
    on every launch, so recording a change there is what makes it outlive a restart.
    Which file that is depends on the config mode, and mngr's own helper owns that
    branch: shared mode gets the managed ``--settings`` overlay, isolated mode gets
    the per-agent config dir's ``settings.json``. The mode is read off whether the
    agent's config dir is its own (inside the state dir) or the host-wide shared one,
    because writing fast mode into the shared config dir would set it for every agent.
    """
    is_config_dir_shared = not claude_config_dir.is_relative_to(agent_state_dir)
    return get_agent_hook_settings_path(agent_state_dir, use_env_config_dir=is_config_dir_shared)


def _read_settings_object(settings_path: Path) -> dict[str, Any]:
    """The settings file's contents as a mutable dict; empty when it does not exist."""
    try:
        raw = settings_path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise FastModeSettingsError(f"Failed to read Claude settings at {settings_path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FastModeSettingsError(f"Claude settings at {settings_path} are not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise FastModeSettingsError(f"Claude settings at {settings_path} are not a JSON object")
    return data


def _write_fast_mode_setting(settings_path: Path, is_enabled: bool) -> None:
    """Record ``fastMode`` in a Claude Code settings file, leaving other keys intact.

    This is the only durable record of the setting: Claude Code deletes the
    ``fastMode`` key on ``/fast off`` rather than writing false, so the session's own
    state is not recoverable from what it writes. mngr owns the file this targets and
    holds its hooks, hence a patch of one key rather than a replacement. Raises
    ``FastModeSettingsError`` when the file exists but is not a JSON object.
    """
    settings = _read_settings_object(settings_path)
    settings["fastMode"] = is_enabled
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(settings_path, json.dumps(settings))


class ClaudeModelResolver(HarnessModelResolver):
    """Switches a Claude agent's model/effort/fast selection (the live read is shared)."""

    _config_dir: Path
    _state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "ClaudeModelResolver":
        self = cls.__new__(cls)
        self._config_dir = agent_info.claude_config_dir
        self._state_dir = agent_info.agent_state_dir
        return self

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        # Model, effort, and fast are three distinct Claude Code commands. Send only
        # the axes the click actually changed -- the frontend computes that against
        # the value the user saw (the optimistic overlay), so a fast toggle does not
        # re-issue /model and /effort, AND re-picking the value you started on
        # (medium -> xhigh -> medium) still sends /effort medium. Diffing here against
        # disk instead would drop that second change whenever disk had not yet reflected
        # the first. Each command lands in the session; the statusline mirrors the
        # effective state to model_state.json, and the watch fires a fresh recompute.
        if ModelAxis.MODEL in axes:
            if not send(f"/model {identity.model_id}"):
                return SwitchResult(ok=False, detail="Failed to deliver /model to the agent")
        if ModelAxis.EFFORT in axes and identity.effort is not None:
            if not send(f"/effort {identity.effort}"):
                return SwitchResult(ok=False, detail="Failed to deliver /effort to the agent")
        if ModelAxis.FAST in axes:
            if not send("/fast on" if identity.fast else "/fast off"):
                return SwitchResult(ok=False, detail="Failed to deliver /fast to the agent")
            # Claude Code leaves no durable record of fast off, so record it into the
            # agent's launch settings -- that is what a restart comes back with.
            write_path = _get_agent_fast_mode_write_path(self._config_dir, self._state_dir)
            try:
                _write_fast_mode_setting(write_path, identity.fast)
            except (FastModeSettingsError, OSError) as e:
                logger.opt(exception=e).error("Failed to record fast mode at {}", write_path)
                return SwitchResult(ok=False, detail="Applied the change but could not record fast mode")
        # The statusline writes the effective {model, effort, fast} on its next fire; the
        # frontend's optimistic overlay covers the gap until then (no UI-side state write).
        return SwitchResult(ok=True)
