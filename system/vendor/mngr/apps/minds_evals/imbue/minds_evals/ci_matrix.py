"""Decide which arms a scheduled run evaluates.

An arm is a frozen (mngr, dwt) pair times one suite's eval config times a named harness config, and
this module turns the four inputs the workflow's free `resolve` job has -- the pairs it froze to
SHAs, the checked-in suite list, the checked-in harness configs file, and the green markers already
in the cache -- into the two job matrices that follow it. Everything a cell's job needs is spelled
out here, so the workflow reads values and composes nothing.

A suite is one eval config and the arms it is worth running on: `configs/nightly_suites.json` is the
single place that names what a night costs. A dispatch can replace it with one ad-hoc suite of its
own, which is what its `config` and `harness_configs` inputs are.

A green marker's key carries the arm: the pair name, both SHAs, the eval config's path, the harness
config's name, and a digest of the parsed harness config. The digest is what makes an edited config
re-run under an unchanged name; the rest is what makes a verified arm skippable. Note what the key
does NOT carry -- the contents of the eval config, the eval harness in this package, or the live
tier every trial boots -- so a marker says this arm was verified once, against whatever harness and
tier existed then, and `--force` is how any of that is re-verified.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from imbue.minds_evals import driver
from imbue.minds_evals.data_types import BehaviourDiagnosticCell
from imbue.minds_evals.data_types import BehaviourHarnessEntry
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import DiagnosticHarnessEntry
from imbue.minds_evals.data_types import FixtureDiagnosticCell
from imbue.minds_evals.data_types import FrozenPair
from imbue.minds_evals.data_types import HarnessConfig
from imbue.minds_evals.data_types import HarnessConfigEntry
from imbue.minds_evals.data_types import HarnessConfigsFile
from imbue.minds_evals.data_types import HarnessName
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import NightlySuite
from imbue.minds_evals.data_types import NightlySuiteArm
from imbue.minds_evals.data_types import NightlySuitesFile
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import ResolvedSuite
from imbue.minds_evals.data_types import SuiteArm
from imbue.minds_evals.data_types import UnsupportedDiagnosticCell
from imbue.minds_evals.data_types import harness_for_lane
from imbue.minds_evals.data_types import harness_id
from imbue.minds_evals.data_types import lane_id
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import CiMatrixError
from imbue.minds_evals.reporting import SHORT_SHA_LENGTH
from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import write_reports

# The harness configs the scheduled run reads when a dispatch names no file of its own.
CHECKED_IN_HARNESS_CONFIGS_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "harness_configs.json"

# What the two self-diagnostic families run on: the fixture family's one config, and the behaviour
# family's config per harness, keyed by the harness as the workspace spells it.
_CHECKED_IN_DIAGNOSTICS_DIR: Final[Path] = CHECKED_IN_HARNESS_CONFIGS_PATH.parent / "diagnostics"
CHECKED_IN_FIXTURE_HARNESS_CONFIG_PATH: Final[Path] = _CHECKED_IN_DIAGNOSTICS_DIR / "fixture_harness_config.json"
CHECKED_IN_BEHAVIOUR_HARNESS_CONFIGS_PATH: Final[Path] = _CHECKED_IN_DIAGNOSTICS_DIR / "behaviour_harness_configs.json"

# The suites the scheduled run evaluates when a dispatch names no config of its own.
CHECKED_IN_NIGHTLY_SUITES_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "nightly_suites.json"

# The checkout a suite's repo-relative config path is resolved against. This package sits two levels
# below `apps/minds_evals`, which sits two below the repository root.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]

# What every green marker's cache key starts with, so a human can list them all with one
# `gh cache list --key` and a run can tell its own markers from anything else in the cache.
GREEN_MARKER_KEY_PREFIX: Final[str] = "minds-evals-green-"

# A config's name becomes a job name, a concurrency group, an artifact name and part of a cache key,
# so it is held to the shape all four accept without quoting or truncation. Dots are in, because a
# model version is part of what names an arm (`pi-glm-4.7-flash`) and all four accept one.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_MAX_NAME_LENGTH: Final[int] = 30

# Every kwarg value below becomes one word of a shell command line: `just minds-evals-run` splices
# the `--ak` arguments into a bash array, so a value carrying whitespace splits into two arguments
# and one carrying a quote or a control character is read by the shell rather than by the driver.
# Permissive about what a value may contain -- `opus[1m]` and `openrouter/openai/gpt-5-mini` are
# both catalog ids -- and strict only about what it may not.
_KWARG_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:\[\]-]*$")

# `key_env` is stricter still: the workflow spells it into its Vault `secrets:` list as
# `mngr/ci/<key_env>` and reads the variable back with `${!var}`, neither of which survives anything
# but an environment variable name.
_KEY_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*$")

# An oracle pass uploads its artifacts as `minds-evals-<kind>-<pair>-<config slug>-oracle...`, and a
# cell of the same suite as `minds-evals-<kind>-<pair>-<config slug>-<harness config>...`. The name
# `oracle` makes the two identical, and both upload with `overwrite: true`, so one would silently
# replace the other. The same word is refused as a config slug, so that a suite cannot reach the
# same collision from the other side.
_RESERVED_NAMES: Final[frozenset[str]] = frozenset({"oracle"})

# The diagnose jobs upload `minds-evals-summary-<pair>-diagnose-fixture` and
# `minds-evals-summary-<pair>-diagnose-behaviour-<harness>`, where a cell uploads
# `minds-evals-summary-<pair>-<config>`: a config named `diagnose` or under this prefix could take a
# diagnose job's artifact name, and every upload overwrites.
DIAGNOSE_NAME: Final[str] = "diagnose"
_DIAGNOSE_NAME_PREFIX: Final[str] = DIAGNOSE_NAME + "-"

# Why a behaviour cell is left out of the matrix. There is one reason a config may give, and it is the
# same one whichever harness gives it, so the report prints it rather than the file spelling one.
UNSUPPORTED_PAIR_REASON: Final[str] = "the pair's workspace template offers this lane no pasted-key sign-in"

# How an error names the fixture family's single config, where a behaviour one is named by its harness.
FIXTURE_FAMILY_NAME: Final[str] = "fixture"

# A suite names its eval config by repo-relative path, held to the same shape the freeze step holds a
# dispatched one to: it is echoed into a green marker key and onto a command line.
_CONFIG_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.json$")

# How long `<pair>-<config slug>-<harness config>` may run. That label names a job in the checks
# list, a concurrency group, two artifacts, a harbor job directory and a summary file, and the
# tightest of those is the checks list, which elides a long job name to the point where two cells of
# one suite cannot be told apart. The pair contributes the longest name the freeze step gives one.
_MAX_CELL_LABEL_LENGTH: Final[int] = 64
_MAX_PAIR_NAME_LENGTH: Final[int] = len("released")

# How many characters of the harness config digest go in the cache key. Long enough that two configs
# in one file cannot collide, short enough to keep the key readable.
_DIGEST_LENGTH: Final[int] = 12


@pure
def harness_config_kwargs(entry: HarnessConfigEntry) -> HarnessConfig:
    """The entry parsed by the driver's own kwarg parsing, which is the only definition of a config
    the run can actually drive.

    The entry's fields are the run line's `--ak` values, so they are handed over as the text an
    operator would have typed: `fast` is None for "say nothing about the speed tier", which the
    driver reads as an empty kwarg, and a bool otherwise.

    Raises AgentKwargError for a combination the workspace would refuse.
    """
    return driver.parse_harness_config(
        lane=entry.lane,
        key_provider=entry.key_provider,
        key_env=entry.key_env,
        model=entry.model,
        effort=entry.effort,
        fast="" if entry.fast is None else str(entry.fast).lower(),
    )


def _check_label(label: str, kind: str, path: Path) -> None:
    """Raises CiMatrixError if the label cannot serve as a job, artifact and cache-key component.

    Both halves of a cell's name -- the harness config's own name and the eval config's slug -- are
    held to this, because both are spelled into all four.
    """
    if not _NAME_PATTERN.match(label):
        raise CiMatrixError(
            "{} {!r} in {} is not lowercase letters, digits, dashes and dots starting with a letter or digit".format(
                kind, label, path
            )
        )
    if len(label) > _MAX_NAME_LENGTH:
        raise CiMatrixError(
            "{} {!r} in {} is {} characters; names become job and artifact names and may be at most {}".format(
                kind, label, path, len(label), _MAX_NAME_LENGTH
            )
        )
    if label in _RESERVED_NAMES:
        raise CiMatrixError(
            "{} {!r} in {} is reserved: a cell of that name would upload its job "
            "directory under the same artifact name as its own oracle pass".format(kind, label, path)
        )
    if label == DIAGNOSE_NAME or label.startswith(_DIAGNOSE_NAME_PREFIX):
        raise CiMatrixError(
            "{} {!r} in {} is reserved: a label {!r} or starting with {!r} could upload its summary "
            "under the artifact name of one of its pair's diagnose jobs".format(
                kind, label, path, DIAGNOSE_NAME, _DIAGNOSE_NAME_PREFIX
            )
        )


def _check_name(name: str, path: Path) -> None:
    """Raises CiMatrixError if the harness config's name cannot name a cell."""
    _check_label(name, "harness config name", path)


def _check_kwarg_values(config_label: str, value_by_field_name: Mapping[str, str], path: Path) -> None:
    """Raises CiMatrixError for a kwarg value that would not survive the run line it rides on.

    An empty value is how a config says nothing about that axis, so only what is given is checked.
    """
    for field_name, value in value_by_field_name.items():
        if value and not _KWARG_VALUE_PATTERN.match(value):
            raise CiMatrixError(
                "harness config {!r} in {} gives {} as {!r}; a kwarg value becomes one word of the run "
                "line and cannot carry whitespace, quotes or shell characters".format(
                    config_label, path, field_name, value
                )
            )


def _check_key_env(key_env: str, config_label: str, path: Path) -> None:
    """Raises CiMatrixError unless the variable a config's key is read from can be one.

    The resolved name, not the field: a config that names no `key_env` has one derived from its lane
    and its `key_provider`, and on the api-key lane that derivation is `<KEY_PROVIDER>_API_KEY` --
    so the derived name inherits whatever the provider carried, and a provider is held to the
    permissive shape a catalog id needs.
    """
    if not _KEY_ENV_PATTERN.match(key_env):
        raise CiMatrixError(
            "harness config {!r} in {} reads its key from {!r}; it names an environment variable and is "
            "spelled into a Vault secret path".format(config_label, path, key_env)
        )


def _load_json_file(path: Path, file_description: str) -> Any:
    """Raises CiMatrixError for a file that cannot be read or is not JSON."""
    try:
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise CiMatrixError("cannot read the {} {}: {}".format(file_description, path, exc)) from exc
    try:
        return json.loads(raw_text)
    except ValueError as exc:
        raise CiMatrixError("the {} {} is not valid JSON: {}".format(file_description, path, exc)) from exc


def load_harness_configs(path: Path) -> tuple[HarnessConfigEntry, ...]:
    """Every named harness config in the file, validated whole.

    Every entry is checked, selected or not, because the file is a shared input: a config nobody
    selected tonight is one somebody dispatches tomorrow, and a name or kwarg combination that only
    fails then fails on a paid runner.

    Raises CiMatrixError for a file, a name, or an entry that could not name a runnable arm.
    """
    payload = _load_json_file(path, "harness configs file")
    try:
        configs_file = HarnessConfigsFile.model_validate(payload)
    except ValidationError as exc:
        raise CiMatrixError("the harness configs file {} is not a harness configs file: {}".format(path, exc)) from exc
    entries = configs_file.harness_configs
    if not entries:
        raise CiMatrixError("the harness configs file {} names no harness configs".format(path))
    seen_names: set[str] = set()
    for entry in entries:
        _check_name(entry.name, path)
        if entry.name in seen_names:
            raise CiMatrixError(
                "the harness configs file {} names {!r} twice; a name has to identify one arm".format(path, entry.name)
            )
        seen_names.add(entry.name)
        _check_kwarg_values(
            entry.name,
            {"lane": entry.lane, "key_provider": entry.key_provider, "model": entry.model, "effort": entry.effort},
            path,
        )
        try:
            parsed_config = harness_config_kwargs(entry)
        except AgentKwargError as exc:
            raise CiMatrixError(
                "harness config {!r} in {} is not one the driver can run: {}".format(entry.name, path, exc)
            ) from exc
        _check_key_env(parsed_config.key_env, entry.name, path)
    return entries


@pure
def select_harness_configs(entries: Sequence[HarnessConfigEntry], selection: str) -> tuple[HarnessConfigEntry, ...]:
    """The configs one run evaluates: those the selection names, or every nightly one when it names
    nothing.

    The file's order is kept whatever order the selection was typed in, and a name repeated in the
    selection still yields one cell, so the same arm cannot be scheduled twice in one run.

    Raises CiMatrixError whenever the run would evaluate nothing -- an empty nightly set, or a
    selection that names no config -- rather than returning an empty selection. A run with no cells
    skips both paid jobs and reports every pair as already green, so a quiet no-op here reads as a
    verified night.
    """
    if not selection.strip():
        nightly = tuple(entry for entry in entries if entry.is_nightly)
        if not nightly:
            raise CiMatrixError(
                "no harness config is marked nightly, so a scheduled run has nothing to evaluate; mark one "
                "or name configs explicitly"
            )
        return nightly
    wanted = {piece.strip() for piece in selection.split(",") if piece.strip()}
    if not wanted:
        raise CiMatrixError(
            "the harness config selection {!r} names no config; leave it empty to run the nightly set".format(
                selection
            )
        )
    known = {entry.name for entry in entries}
    unknown = sorted(wanted - known)
    if unknown:
        raise CiMatrixError(
            "no harness config is named {}; the file holds {}".format(", ".join(unknown), ", ".join(sorted(known)))
        )
    return tuple(entry for entry in entries if entry.name in wanted)


@pure
def select_nightly_harnesses(entries: Sequence[HarnessConfigEntry]) -> tuple[HarnessName, ...]:
    """The distinct harnesses the nightly configs run on, in the order the file first names each.

    A behaviour diagnostic runs one cell per harness here, so each harness a night measures has its
    reader checked, whichever of its configs are nightly.

    Raises CiMatrixError when no config is nightly, as `select_harness_configs` does.
    """
    nightly_harnesses = (
        harness_for_lane(harness_config_kwargs(entry).lane) for entry in select_harness_configs(entries, "")
    )
    return tuple(dict.fromkeys(nightly_harnesses))


def diagnostic_harness_config_kwargs(entry: DiagnosticHarnessEntry) -> HarnessConfig:
    """A diagnostic family's entry parsed by the driver's own kwarg parsing, the way a named config is.

    Raises AgentKwargError for a combination the workspace would refuse.
    """
    return driver.parse_harness_config(
        lane=entry.lane,
        key_provider=entry.key_provider,
        key_env=entry.key_env,
        model=entry.model,
        effort=entry.effort,
        fast=entry.fast,
    )


def _check_diagnostic_harness_entry(entry: DiagnosticHarnessEntry, config_label: str, path: Path) -> HarnessConfig:
    """The entry as the driver parses it, refused here on the free job rather than on a paid runner.

    Raises CiMatrixError for kwargs the run line could not carry or the driver would not accept.
    """
    _check_kwarg_values(
        config_label,
        {"lane": entry.lane, "key_provider": entry.key_provider, "model": entry.model, "effort": entry.effort},
        path,
    )
    try:
        parsed = diagnostic_harness_config_kwargs(entry)
    except AgentKwargError as exc:
        raise CiMatrixError(
            "diagnostic harness config {!r} in {} is not one the driver can run: {}".format(config_label, path, exc)
        ) from exc
    _check_key_env(parsed.key_env, config_label, path)
    return parsed


def load_fixture_harness_config(path: Path) -> DiagnosticHarnessEntry:
    """The fixture family's entry, validated through the driver's own kwarg parsing.

    Raises CiMatrixError for a file that is not one runnable harness config.
    """
    payload = _load_json_file(path, "fixture harness config")
    try:
        entry = DiagnosticHarnessEntry.model_validate(payload)
    except ValidationError as exc:
        raise CiMatrixError(
            "the fixture harness config {} is not a set of `--ak` kwargs: {}".format(path, exc)
        ) from exc
    _check_diagnostic_harness_entry(entry, FIXTURE_FAMILY_NAME, path)
    return entry


def load_behaviour_harness_configs(path: Path) -> dict[HarnessName, BehaviourHarnessEntry]:
    """The behaviour family's entry per harness, every one validated whether or not a night runs it.

    Raises CiMatrixError for a file that is not an object of runnable harness configs, keyed each by
    the harness its lane actually runs.
    """
    payload = _load_json_file(path, "behaviour harness configs file")
    if not isinstance(payload, dict):
        raise CiMatrixError(
            "the behaviour harness configs file {} is a {}, not an object keyed by harness".format(
                path, type(payload).__name__
            )
        )
    harness_by_id = {harness_id(harness): harness for harness in HarnessName}
    entry_by_harness: dict[HarnessName, BehaviourHarnessEntry] = {}
    for harness_key, raw_entry in payload.items():
        harness = harness_by_id.get(harness_key)
        if harness is None:
            raise CiMatrixError(
                "the behaviour harness configs file {} names {!r}, which is no harness; expected one of {}".format(
                    path, harness_key, ", ".join(sorted(harness_by_id))
                )
            )
        try:
            entry = BehaviourHarnessEntry.model_validate(raw_entry)
        except ValidationError as exc:
            raise CiMatrixError(
                "behaviour harness config {!r} in {} is not a set of `--ak` kwargs: {}".format(harness_key, path, exc)
            ) from exc
        parsed = _check_diagnostic_harness_entry(entry, harness_key, path)
        lane_harness = harness_for_lane(parsed.lane)
        if lane_harness is not harness:
            raise CiMatrixError(
                "behaviour harness config {!r} in {} signs in on lane {}, whose chats run {}".format(
                    harness_key, path, lane_id(parsed.lane), harness_id(lane_harness)
                )
            )
        entry_by_harness[harness] = entry
    return entry_by_harness


@pure
def select_behaviour_harness_configs(
    entry_by_harness: Mapping[HarnessName, BehaviourHarnessEntry], harnesses: Sequence[HarnessName]
) -> tuple[tuple[HarnessName, BehaviourHarnessEntry], ...]:
    """The behaviour entry of each harness a night measures, in the order given.

    Raises CiMatrixError for a harness with no entry: a nightly harness without one would get no
    behaviour cell, and the readers only it exercises would go unchecked in silence.
    """
    missing = [harness_id(harness) for harness in harnesses if harness not in entry_by_harness]
    if missing:
        raise CiMatrixError(
            "the nightly harness configs run on {} but the behaviour harness configs name no config for {}".format(
                ", ".join(harness_id(harness) for harness in harnesses), ", ".join(missing)
            )
        )
    return tuple((harness, entry_by_harness[harness]) for harness in harnesses)


@pure
def config_slug(config_path: str) -> str:
    """The eval config's one-word name: its file name, lowercased, with runs of anything that is not
    a letter, a digit or a dot turned into single dashes.

    A job name, a concurrency group, two artifact names and a summary file name all carry it beside
    the pair and the arm, and the path itself carries slashes and an extension that none of them
    take. The file name alone rather than the whole path, because that is the part a reader
    recognises in a checks list -- which is also why two suites may not name two configs whose file
    names shorten to the same word.
    """
    return re.sub(r"[^a-z0-9.]+", "-", Path(config_path).stem.lower()).strip("-")


def _check_dispatched_config_path(config_path: str) -> None:
    """Raises CiMatrixError unless a dispatched eval config is shaped like a repo-relative json path.

    Only the shape. Whether the file is in the checkout is the workflow's own check, on the same
    free job, so that a typo is reported against the input the operator typed.
    """
    if not _CONFIG_PATH_PATTERN.match(config_path):
        raise CiMatrixError("the eval config {!r} is not a repo-relative .json path".format(config_path))


def load_nightly_suites(path: Path, repo_root: Path) -> tuple[NightlySuite, ...]:
    """Every suite a scheduled run evaluates, validated whole.

    An arm's name is checked against the harness configs file in `resolve_suites`, where that file
    is in hand. What is checked here is everything this file can be wrong about on its own: a config
    that is not a repo-relative json path, one that is not in the checkout, the same config in two
    suites, and two configs whose file names shorten to one word and would share every job and
    artifact name.

    Raises CiMatrixError for a file, a config, or a slug that could not name a suite.
    """
    try:
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise CiMatrixError("cannot read the nightly suites file {}: {}".format(path, exc)) from exc
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        raise CiMatrixError("the nightly suites file {} is not valid JSON: {}".format(path, exc)) from exc
    try:
        suites_file = NightlySuitesFile.model_validate(payload)
    except ValidationError as exc:
        raise CiMatrixError("the nightly suites file {} is not a nightly suites file: {}".format(path, exc)) from exc
    suites = suites_file.suites
    if not suites:
        raise CiMatrixError("the nightly suites file {} names no suites".format(path))
    seen_configs: set[str] = set()
    configs_by_slug: dict[str, str] = {}
    for suite in suites:
        if not _CONFIG_PATH_PATTERN.match(suite.config):
            raise CiMatrixError(
                "the nightly suites file {} names the config {!r}; a suite's config is a repo-relative "
                ".json path".format(path, suite.config)
            )
        if not (repo_root / suite.config).is_file():
            raise CiMatrixError(
                "the nightly suites file {} names the config {!r}, which is not in the checkout at {}".format(
                    path, suite.config, repo_root
                )
            )
        if suite.config in seen_configs:
            raise CiMatrixError(
                "the nightly suites file {} names the config {!r} twice; one suite holds all of a "
                "config's arms".format(path, suite.config)
            )
        seen_configs.add(suite.config)
        slug = config_slug(suite.config)
        _check_label(slug, "eval config slug", path)
        if slug in configs_by_slug:
            raise CiMatrixError(
                "the nightly suites file {} names {!r} and {!r}, whose file names both shorten to {!r}: "
                "the two would share every job, artifact and summary name".format(
                    path, configs_by_slug[slug], suite.config, slug
                )
            )
        configs_by_slug[slug] = suite.config
    return suites


@pure
def _dispatched_arms(entries: Sequence[HarnessConfigEntry], selection: str) -> tuple[NightlySuiteArm, ...]:
    """The arms a dispatched selection names, at one attempt each: attempts are a suite's own
    decision, and a dispatch form has nowhere to type them."""
    return tuple(NightlySuiteArm(name=entry.name) for entry in select_harness_configs(entries, selection))


@pure
def suites_for_run(
    *,
    suites: Sequence[NightlySuite],
    entries: Sequence[HarnessConfigEntry],
    config_path: str,
    selection: str,
) -> tuple[NightlySuite, ...]:
    """Which suites this run evaluates: the checked-in list, or what a dispatch asked for instead.

    A dispatch that names a config replaces the list with that config alone, run on the harness
    configs it named or, naming none, on the nightly set. One that names only harness configs keeps
    the checked-in configs and runs every one of them on exactly those arms, which is what makes
    "try this arm tonight" one input to type. A schedule names neither and gets the file as written.

    Raises CiMatrixError for a config or a selection that could not name a suite.
    """
    if config_path:
        _check_dispatched_config_path(config_path)
        return (NightlySuite(config=config_path, harness_configs=_dispatched_arms(entries, selection)),)
    if selection.strip():
        return tuple(
            NightlySuite(config=suite.config, harness_configs=_dispatched_arms(entries, selection)) for suite in suites
        )
    return tuple(suites)


@pure
def resolve_suites(suites: Sequence[NightlySuite], entries: Sequence[HarnessConfigEntry]) -> tuple[ResolvedSuite, ...]:
    """Every suite with its arms looked up in the harness configs file.

    A suite that names no arm runs the nightly set in the harness configs file's order; one that
    names arms runs exactly those, in the order it wrote them, whatever their nightly flag says --
    an arm too expensive to run against every config is still worth one suite naming it.

    Raises CiMatrixError for an arm the harness configs file does not hold, for a suite naming one
    arm twice, and for a cell whose name would not fit where it is reported.
    """
    entries_by_name = {entry.name: entry for entry in entries}
    resolved: list[ResolvedSuite] = []
    for suite in suites:
        slug = config_slug(suite.config)
        arms: list[SuiteArm] = []
        seen_names: set[str] = set()
        for arm in suite.harness_configs or _dispatched_arms(entries, ""):
            entry = entries_by_name.get(arm.name)
            if entry is None:
                raise CiMatrixError(
                    "the suite for {} names the harness config {!r}, which the harness configs file does "
                    "not hold; it holds {}".format(suite.config, arm.name, ", ".join(sorted(entries_by_name)))
                )
            if arm.name in seen_names:
                raise CiMatrixError(
                    "the suite for {} names the harness config {!r} twice; two cells of one arm share a "
                    "concurrency group and a green marker key".format(suite.config, arm.name)
                )
            seen_names.add(arm.name)
            label_length = _MAX_PAIR_NAME_LENGTH + len(slug) + len(arm.name) + 2
            if label_length > _MAX_CELL_LABEL_LENGTH:
                raise CiMatrixError(
                    "the suite for {} runs the harness config {!r}, whose cell is named "
                    "'<pair>-{}-{}': {} characters with the longest pair name, and a cell's name may be "
                    "at most {}".format(suite.config, arm.name, slug, arm.name, label_length, _MAX_CELL_LABEL_LENGTH)
                )
            arms.append(SuiteArm(entry=entry, attempts=arm.attempts))
        resolved.append(ResolvedSuite(config=suite.config, config_slug=slug, arms=tuple(arms)))
    return tuple(resolved)


def read_frozen_pairs(path: Path) -> tuple[FrozenPair, ...]:
    """The pairs the freeze step wrote, one JSON object per line.

    A file with no lines is a run that froze no pair, which is reported rather than refused: the
    freeze step decides which pairs a dispatch asked about, and "none" is one of its answers.

    Raises CiMatrixError for a line that is not a frozen pair, and for a pair name that appears
    twice: a repeated name yields two cells with one concurrency group and one green marker key
    between them, and nothing downstream is positioned to notice.
    """
    try:
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise CiMatrixError("cannot read the frozen pairs file {}: {}".format(path, exc)) from exc
    pairs: list[FrozenPair] = []
    seen_names: set[str] = set()
    for line_number, line in enumerate(raw_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            pair = FrozenPair.model_validate_json(line)
        except ValidationError as exc:
            raise CiMatrixError("{} line {} is not a frozen pair: {}".format(path, line_number, exc)) from exc
        if pair.pair in seen_names:
            raise CiMatrixError(
                "{} line {} freezes {!r} again; a pair name has to identify one pair".format(
                    path, line_number, pair.pair
                )
            )
        seen_names.add(pair.pair)
        pairs.append(pair)
    return tuple(pairs)


def read_green_marker_keys(path: Path, restorable_refs: Sequence[str]) -> frozenset[str]:
    """The green markers this run is allowed to read, out of `gh cache list --json key,ref` output.

    The ref filter is the whole point of listing rather than looking each key up: GitHub's cache
    restore serves a run only the entries saved on its own ref or on the default branch, so a
    listing taken without that filter would let a marker saved by a feature-branch push skip a cell
    on main -- an arm reported green that this run could never have restored.

    Raises CiMatrixError if the file is not the JSON array of objects that command prints.
    """
    try:
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise CiMatrixError("cannot read the green markers file {}: {}".format(path, exc)) from exc
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise CiMatrixError("the green markers file {} is not valid JSON: {}".format(path, exc)) from exc
    if not isinstance(parsed, list):
        raise CiMatrixError(
            "the green markers file {} is a {}, not the JSON array `gh cache list --json key,ref` prints".format(
                path, type(parsed).__name__
            )
        )
    allowed_refs = frozenset(restorable_refs)
    keys: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            raise CiMatrixError(
                "the green markers file {} holds a {}, not a cache entry object".format(path, type(entry).__name__)
            )
        key = entry.get("key")
        ref = entry.get("ref")
        if not isinstance(key, str) or not isinstance(ref, str):
            logger.warning("Ignoring a cache entry in {} that carries no readable key and ref: {}", path, entry)
            continue
        if ref in allowed_refs:
            keys.add(key)
    return frozenset(keys)


@pure
def harbor_args_for(entry: HarnessConfigEntry) -> tuple[str, ...]:
    """The `--ak` arguments a cell appends to its harbor run line, as a flat argv tuple."""
    return harbor_args_for_harness_config(harness_config_kwargs(entry))


@pure
def harbor_args_for_harness_config(parsed: HarnessConfig) -> tuple[str, ...]:
    """The `--ak` arguments that drive a parsed harness config, as a flat argv tuple.

    Built from the parsed config rather than a file's fields, so what a job runs on is exactly what
    was validated. Only a config that names a model carries the model axes: the driver refuses
    `effort` or `fast` without one, and a config that names no model must leave the workspace's own
    model and speed tier alone -- that is what makes the default arm the product as it ships.
    """
    args = ["--ak", "lane={}".format(lane_id(parsed.lane)), "--ak", "key_env={}".format(parsed.key_env)]
    if parsed.key_provider:
        args += ["--ak", "key_provider={}".format(parsed.key_provider)]
    if parsed.model:
        args += [
            "--ak",
            "model={}".format(parsed.model),
            "--ak",
            "effort={}".format(parsed.effort),
            "--ak",
            "fast={}".format("true" if parsed.is_fast else "false"),
        ]
    return tuple(args)


@pure
def _dashed_config_path(config_path: str) -> str:
    """The eval config's whole path as a cache-key component, so the key stays one word whatever path
    a dispatch names.

    The whole path, unlike the `config_slug` a job name carries: a key is read by machines and by
    whoever lists the markers, and two configs of one file name under different directories are two
    arms.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", config_path)


@pure
def cache_key_for(pair: FrozenPair, config_path: str, entry: HarnessConfigEntry) -> str:
    """The cache key of one arm's green marker.

    The digest over the parsed harness config is what the name alone cannot say: editing a config's
    model or lane in place, under an unchanged name, changes the arm, and the edited arm has never
    been verified. Canonical JSON so that a reordered file does not move the key.

    A suite's attempt count is deliberately absent: it says how many samples of an arm a night buys,
    not which arm ran, so an arm verified at one attempt stays verified when a suite asks for three.
    """
    parsed = harness_config_kwargs(entry)
    canonical = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:_DIGEST_LENGTH]
    return "{}{}-mngr-{}-dwt-{}-cfg-{}-hc-{}-{}".format(
        GREEN_MARKER_KEY_PREFIX,
        pair.pair,
        pair.mngr_sha,
        pair.dwt_sha,
        _dashed_config_path(config_path),
        entry.name,
        digest,
    )


@pure
def _decide_cells(
    pair: FrozenPair,
    suite: ResolvedSuite,
    green_keys: frozenset[str],
    is_forced: bool,
) -> tuple[MatrixCell, ...]:
    cells: list[MatrixCell] = []
    for arm in suite.arms:
        parsed = harness_config_kwargs(arm.entry)
        cache_key = cache_key_for(pair, suite.config, arm.entry)
        is_green = cache_key in green_keys and not is_forced
        cells.append(
            MatrixCell(
                pair=pair.pair,
                harness_config=arm.entry.name,
                mngr_ref=pair.mngr_ref,
                mngr_sha=pair.mngr_sha,
                dwt_ref=pair.dwt_ref,
                dwt_sha=pair.dwt_sha,
                config=suite.config,
                config_slug=suite.config_slug,
                attempts=arm.attempts,
                lane_key_env=parsed.key_env,
                harbor_args=json.dumps(list(harbor_args_for(arm.entry))),
                cache_key=cache_key,
                decision=CellDecision.SKIP if is_green else CellDecision.RUN,
            )
        )
    return tuple(cells)


@pure
def decide_matrix(
    *,
    pairs: Sequence[FrozenPair],
    suites: Sequence[ResolvedSuite],
    green_keys: frozenset[str],
    is_forced: bool,
    fixture_harness_config: DiagnosticHarnessEntry,
    behaviour_harness_configs: Sequence[tuple[HarnessName, BehaviourHarnessEntry]],
) -> CiMatrix:
    """Every arm the run considered and what it decided about each, and the diagnose jobs of every
    resolved pair.

    A pair whose refs did not resolve has no SHAs to key a marker on and nothing to check out, so it
    gets no cells at all and is reported as unresolved; the other pairs still run, because the pairs
    answer different questions and losing one answer to another's missing ref would give up the one
    that matters most. A resolved pair runs when any of its cells does, and each suite it has a
    running cell in gets an oracle pass of its own.

    The pairs are the outer loop, so a pair's cells stay together in the summary table and in the
    matrix a job fans out over.

    Every resolved pair gets its diagnostics whatever its cells decided: an all-green night moves no
    SHA the diagnostics would notice, and is exactly the night the instrument still has to be checked.
    A behaviour cell whose harness config names the pair as unsupported is left out rather than run
    against a template that cannot sign its lane in, and is reported instead.
    """
    decided_pairs: list[DecidedPair] = []
    all_cells: list[MatrixCell] = []
    fixture_diagnostics: list[FixtureDiagnosticCell] = []
    behaviour_diagnostics: list[BehaviourDiagnosticCell] = []
    unsupported_diagnostics: list[UnsupportedDiagnosticCell] = []
    fixture_config = diagnostic_harness_config_kwargs(fixture_harness_config)
    fixture_harbor_args = json.dumps(list(harbor_args_for_harness_config(fixture_config)))
    for pair in pairs:
        if not pair.is_resolved:
            decided_pairs.append(DecidedPair(**pair.model_dump(), decision=PairDecision.UNRESOLVED))
            continue
        cells = tuple(cell for suite in suites for cell in _decide_cells(pair, suite, green_keys, is_forced))
        all_cells.extend(cells)
        is_any_running = any(cell.decision is CellDecision.RUN for cell in cells)
        decided_pairs.append(
            DecidedPair(**pair.model_dump(), decision=PairDecision.RUN if is_any_running else PairDecision.SKIP)
        )
        fixture_diagnostics.append(
            FixtureDiagnosticCell(
                **pair.model_dump(), lane_key_env=fixture_config.key_env, harbor_args=fixture_harbor_args
            )
        )
        for harness, entry in behaviour_harness_configs:
            if pair.pair in entry.unsupported_pairs:
                unsupported_diagnostics.append(
                    UnsupportedDiagnosticCell(
                        pair=pair.pair, harness=harness_id(harness), reason=UNSUPPORTED_PAIR_REASON
                    )
                )
                continue
            parsed = diagnostic_harness_config_kwargs(entry)
            behaviour_diagnostics.append(
                BehaviourDiagnosticCell(
                    **pair.model_dump(),
                    harness=harness_id(harness),
                    lane_key_env=parsed.key_env,
                    harbor_args=json.dumps(list(harbor_args_for_harness_config(parsed))),
                )
            )
    return CiMatrix(
        configs=tuple(suite.config for suite in suites),
        pairs=tuple(decided_pairs),
        cells=tuple(all_cells),
        fixture_diagnostics=tuple(fixture_diagnostics),
        behaviour_diagnostics=tuple(behaviour_diagnostics),
        unsupported_diagnostics=tuple(unsupported_diagnostics),
    )


@pure
def _short_sha(sha: str) -> str:
    """A SHA for the table, or the word an unresolved pair prints instead: it has refs but no SHAs,
    and an empty pair of backticks would read as a pair whose SHA went unrecorded."""
    return sha[:SHORT_SHA_LENGTH] if sha else "unresolved"


@pure
def _marker_listing_hint(repository: str) -> str:
    """The command that lists every green marker. Without a repository it still works, from a
    checkout of the repository itself."""
    if repository:
        return "gh cache list --repo {} --key {}".format(repository, GREEN_MARKER_KEY_PREFIX)
    return "gh cache list --key {}".format(GREEN_MARKER_KEY_PREFIX)


@pure
def render_matrix_summary_markdown(matrix: CiMatrix, repository: str) -> str:
    """The decision as a GitHub step-summary table: one row per arm, plus one per pair that never
    resolved into arms at all."""
    lines = [
        "## Arms",
        "",
        "| pair | config | harness config | mngr | dwt | decision |",
        "|---|---|---|---|---|---|",
    ]
    cells_by_pair: dict[str, list[MatrixCell]] = {}
    for cell in matrix.cells:
        cells_by_pair.setdefault(cell.pair, []).append(cell)
    for pair in matrix.pairs:
        if pair.decision is PairDecision.UNRESOLVED:
            lines.append(
                "| `{}` | - | - | `{}` (`{}`) | `{}` (`{}`) | **unresolved** |".format(
                    as_table_cell(pair.pair),
                    as_table_cell(pair.mngr_ref),
                    _short_sha(pair.mngr_sha),
                    as_table_cell(pair.dwt_ref),
                    _short_sha(pair.dwt_sha),
                )
            )
            continue
        for cell in cells_by_pair.get(pair.pair, []):
            lines.append(
                "| `{}` | `{}` | `{}` | `{}` (`{}`) | `{}` (`{}`) | **{}** |".format(
                    as_table_cell(cell.pair),
                    as_table_cell(cell.config_slug),
                    as_table_cell(cell.harness_config),
                    as_table_cell(cell.mngr_ref),
                    _short_sha(cell.mngr_sha),
                    as_table_cell(cell.dwt_ref),
                    _short_sha(cell.dwt_sha),
                    cell.decision.value,
                )
            )
    lines += [
        "",
        "- configs: {}".format(", ".join("`{}`".format(as_table_cell(config)) for config in matrix.configs)),
        "- a `skip` means the green marker already holds that exact arm: the pair's mngr SHA and dwt SHA, the "
        "config, and the harness config; dispatch with force=true to re-run it",
        "- an `unresolved` means one of that pair's refs does not exist on its remote; the other pairs still run",
        "",
        "Inspect the green markers (the Caches web UI cannot filter by key prefix):",
        "",
        "```",
        _marker_listing_hint(repository),
        "```",
        "",
        "## Diagnostics",
        "",
        "| pair | family | harness | decision |",
        "|---|---|---|---|",
        *(
            "| `{}` | fixture | - | **run** |".format(as_table_cell(diagnostic.pair))
            for diagnostic in matrix.fixture_diagnostics
        ),
        *(
            "| `{}` | behaviour | `{}` | **run** |".format(
                as_table_cell(diagnostic.pair), as_table_cell(diagnostic.harness)
            )
            for diagnostic in matrix.behaviour_diagnostics
        ),
        *(
            "| `{}` | behaviour | `{}` | **unsupported**: {} |".format(
                as_table_cell(diagnostic.pair), as_table_cell(diagnostic.harness), as_table_cell(diagnostic.reason)
            )
            for diagnostic in matrix.unsupported_diagnostics
        ),
        "",
        "- every resolved pair runs its diagnose jobs whatever its cells decided: they write no green marker, "
        "read none, and gate nothing",
        "- an `unsupported` behaviour cell is not run at all, and the Slack report names it: no box is spent to "
        "produce a dark cell",
    ]
    return "\n".join(lines) + "\n"


def write_matrix_reports(matrix: CiMatrix, output_path: Path, summary_md_path: Path | None, repository: str) -> None:
    """Write the decided matrix the jobs after this one read, and, when asked, the human summary."""
    write_reports(
        [
            (output_path, matrix.model_dump_json(indent=2) + "\n"),
            (summary_md_path, render_matrix_summary_markdown(matrix, repository)),
        ]
    )
