"""Decide which arms a scheduled run evaluates.

An arm is a frozen (mngr, dwt) pair times a named harness config, and this module turns the three
inputs the workflow's free `resolve` job has -- the pairs it froze to SHAs, the checked-in harness
configs file, and the green markers already in the cache -- into the two job matrices that follow
it. Everything a cell's job needs is spelled out here, so the workflow reads values and composes
nothing.

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
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from imbue.minds_evals import driver
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import FrozenPair
from imbue.minds_evals.data_types import HarnessConfig
from imbue.minds_evals.data_types import HarnessConfigEntry
from imbue.minds_evals.data_types import HarnessConfigsFile
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import lane_id
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import CiMatrixError
from imbue.minds_evals.reporting import SHORT_SHA_LENGTH
from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import write_reports

# The harness configs the scheduled run reads when a dispatch names no file of its own.
CHECKED_IN_HARNESS_CONFIGS_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "harness_configs.json"

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

# The oracle pass uploads its job directory as `minds-evals-jobs-<pair>-oracle-<run id>`, and a cell
# uploads its own as `minds-evals-jobs-<pair>-<config>-<run id>`. A config called `oracle` makes the
# two names identical, and both upload with `overwrite: true`, so one silently replaces the other.
_RESERVED_NAMES: Final[frozenset[str]] = frozenset({"oracle"})

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


def _check_name(name: str, path: Path) -> None:
    """Raises CiMatrixError if the name cannot serve as a job, artifact and cache-key component."""
    if not _NAME_PATTERN.match(name):
        raise CiMatrixError(
            "harness config name {!r} in {} is not lowercase letters, digits, dashes and dots starting "
            "with a letter or digit".format(name, path)
        )
    if len(name) > _MAX_NAME_LENGTH:
        raise CiMatrixError(
            "harness config name {!r} in {} is {} characters; names become job and artifact names and may "
            "be at most {}".format(name, path, len(name), _MAX_NAME_LENGTH)
        )
    if name in _RESERVED_NAMES:
        raise CiMatrixError(
            "harness config name {!r} in {} is reserved: a cell of that name would upload its job "
            "directory under the same artifact name as its pair's oracle pass".format(name, path)
        )


def _check_kwarg_values(entry: HarnessConfigEntry, path: Path) -> None:
    """Raises CiMatrixError for a kwarg value that would not survive the run line it rides on.

    An empty value is how an entry says nothing about that axis, so only what is given is checked.
    """
    for field_name, value in (
        ("lane", entry.lane),
        ("key_provider", entry.key_provider),
        ("model", entry.model),
        ("effort", entry.effort),
    ):
        if value and not _KWARG_VALUE_PATTERN.match(value):
            raise CiMatrixError(
                "harness config {!r} in {} gives {} as {!r}; a kwarg value becomes one word of the run "
                "line and cannot carry whitespace, quotes or shell characters".format(
                    entry.name, path, field_name, value
                )
            )


def _check_key_env(key_env: str, entry: HarnessConfigEntry, path: Path) -> None:
    """Raises CiMatrixError unless the variable a config's key is read from can be one.

    The resolved name, not the field: an entry that names no `key_env` has one derived from its lane
    and its `key_provider`, and on the api-key lane that derivation is `<KEY_PROVIDER>_API_KEY` --
    so the derived name inherits whatever the provider carried, and a provider is held to the
    permissive shape a catalog id needs.
    """
    if not _KEY_ENV_PATTERN.match(key_env):
        raise CiMatrixError(
            "harness config {!r} in {} reads its key from {!r}; it names an environment variable and is "
            "spelled into a Vault secret path".format(entry.name, path, key_env)
        )


def load_harness_configs(path: Path) -> tuple[HarnessConfigEntry, ...]:
    """Every named harness config in the file, validated whole.

    Every entry is checked, selected or not, because the file is a shared input: a config nobody
    selected tonight is one somebody dispatches tomorrow, and a name or kwarg combination that only
    fails then fails on a paid runner.

    Raises CiMatrixError for a file, a name, or an entry that could not name a runnable arm.
    """
    try:
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise CiMatrixError("cannot read the harness configs file {}: {}".format(path, exc)) from exc
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        raise CiMatrixError("the harness configs file {} is not valid JSON: {}".format(path, exc)) from exc
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
        _check_kwarg_values(entry, path)
        try:
            parsed_config = harness_config_kwargs(entry)
        except AgentKwargError as exc:
            raise CiMatrixError(
                "harness config {!r} in {} is not one the driver can run: {}".format(entry.name, path, exc)
            ) from exc
        _check_key_env(parsed_config.key_env, entry, path)
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
    """The `--ak` arguments a cell appends to its harbor run line, as a flat argv tuple.

    Built from the parsed config rather than the file's fields, so what a cell runs on is exactly
    what was validated. Only a config that names a model carries the model axes: the driver refuses
    `effort` or `fast` without one, and a config that names no model must leave the workspace's own
    model and speed tier alone -- that is what makes the default arm the product as it ships.
    """
    parsed = harness_config_kwargs(entry)
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
def _config_slug(config_path: str) -> str:
    """The eval config's path as a cache-key component, so the key stays one word whatever path a
    dispatch names."""
    return re.sub(r"[^A-Za-z0-9]", "-", config_path)


@pure
def cache_key_for(pair: FrozenPair, config_path: str, entry: HarnessConfigEntry) -> str:
    """The cache key of one arm's green marker.

    The digest over the parsed harness config is what the name alone cannot say: editing a config's
    model or lane in place, under an unchanged name, changes the arm, and the edited arm has never
    been verified. Canonical JSON so that a reordered file does not move the key.
    """
    parsed = harness_config_kwargs(entry)
    canonical = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:_DIGEST_LENGTH]
    return "{}{}-mngr-{}-dwt-{}-cfg-{}-hc-{}-{}".format(
        GREEN_MARKER_KEY_PREFIX,
        pair.pair,
        pair.mngr_sha,
        pair.dwt_sha,
        _config_slug(config_path),
        entry.name,
        digest,
    )


@pure
def _decide_cells(
    pair: FrozenPair,
    entries: Sequence[HarnessConfigEntry],
    config_path: str,
    green_keys: frozenset[str],
    is_forced: bool,
) -> tuple[MatrixCell, ...]:
    cells: list[MatrixCell] = []
    for entry in entries:
        parsed = harness_config_kwargs(entry)
        cache_key = cache_key_for(pair, config_path, entry)
        is_green = cache_key in green_keys and not is_forced
        cells.append(
            MatrixCell(
                pair=pair.pair,
                harness_config=entry.name,
                mngr_ref=pair.mngr_ref,
                mngr_sha=pair.mngr_sha,
                dwt_ref=pair.dwt_ref,
                dwt_sha=pair.dwt_sha,
                config=config_path,
                lane_key_env=parsed.key_env,
                harbor_args=json.dumps(list(harbor_args_for(entry))),
                cache_key=cache_key,
                decision=CellDecision.SKIP if is_green else CellDecision.RUN,
            )
        )
    return tuple(cells)


@pure
def decide_matrix(
    *,
    pairs: Sequence[FrozenPair],
    entries: Sequence[HarnessConfigEntry],
    config_path: str,
    green_keys: frozenset[str],
    is_forced: bool,
) -> CiMatrix:
    """Every arm the run considered and what it decided about each.

    A pair whose refs did not resolve has no SHAs to key a marker on and nothing to check out, so it
    gets no cells at all and is reported as unresolved; the other pairs still run, because the pairs
    answer different questions and losing one answer to another's missing ref would give up the one
    that matters most. A resolved pair runs when any of its cells does, since its oracle pass serves
    all of them.
    """
    decided_pairs: list[DecidedPair] = []
    all_cells: list[MatrixCell] = []
    for pair in pairs:
        if not pair.is_resolved:
            decided_pairs.append(DecidedPair(**pair.model_dump(), decision=PairDecision.UNRESOLVED))
            continue
        cells = _decide_cells(pair, entries, config_path, green_keys, is_forced)
        all_cells.extend(cells)
        is_any_running = any(cell.decision is CellDecision.RUN for cell in cells)
        decided_pairs.append(
            DecidedPair(**pair.model_dump(), decision=PairDecision.RUN if is_any_running else PairDecision.SKIP)
        )
    return CiMatrix(config=config_path, pairs=tuple(decided_pairs), cells=tuple(all_cells))


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
    lines = ["## Arms", "", "| pair | harness config | mngr | dwt | decision |", "|---|---|---|---|---|"]
    cells_by_pair: dict[str, list[MatrixCell]] = {}
    for cell in matrix.cells:
        cells_by_pair.setdefault(cell.pair, []).append(cell)
    for pair in matrix.pairs:
        if pair.decision is PairDecision.UNRESOLVED:
            lines.append(
                "| `{}` | - | `{}` (`{}`) | `{}` (`{}`) | **unresolved** |".format(
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
                "| `{}` | `{}` | `{}` (`{}`) | `{}` (`{}`) | **{}** |".format(
                    as_table_cell(cell.pair),
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
        "- config: `{}`".format(as_table_cell(matrix.config)),
        "- a `skip` means the green marker already holds that exact arm: the pair's mngr SHA and dwt SHA, the "
        "config, and the harness config; dispatch with force=true to re-run it",
        "- an `unresolved` means one of that pair's refs does not exist on its remote; the other pairs still run",
        "",
        "Inspect the green markers (the Caches web UI cannot filter by key prefix):",
        "",
        "```",
        _marker_listing_hint(repository),
        "```",
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
