"""Read a finished harbor job directory and decide whether the run passed.

A scheduled run has to answer one question in one exit code, and the artifacts that answer it are
spread across four files per trial: harbor's own ``result.json`` (did the trial run at all), the
driver's ``agent/state.json`` (did the conversation reach its end, or run out of time -- harbor
grades a timed-out trial as an ordinary result, so nothing else tells the two apart),
``verifier/reward-details.json`` (did the structural gates hold, what did the judges say) and
``agent/verification/manifest.json`` (was anything left unmeasured).

The gate is deliberately narrow. A trial is charged for not running, for failing a structural gate,
and for observably running on a model other than the one its harness config asked for; it is never
charged for a judge score, which is statistical and drifts between runs. An `error` in the evidence
manifest fails the run because it is the harness that broke, not the workspace -- the same `failed`
versus `error` split the collector and the verifier already rest on.

Which file each of those is, though, is not the trial root for every trial: harbor relocates a
stepped trial's artifacts into `steps/<name>/` as it goes. Every path read here therefore comes from
`trial_layout`, which places each artifact by what it means -- the last step's cumulative state and
cost account, every step's own evidence manifest and reward details.

A fifth file is read for the record alone. ``agent/usage.json`` is the driver's per-spender token
account, and this is where it turns into money: a trial records what each spender consumed and
nothing about what it cost, so every figure in both summaries is priced here, from the price map
``check_job_directory`` is handed, and the report says which map answered. None of it gates anything
-- a cost is not a shortfall, and a run that went red because it got expensive would be a run nobody
could interpret. Every figure keeps the flags that say how far it can be read, because a cost
stripped of them reads as exact.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from harbor.models.trial.result import TrialResult
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import JudgeScore
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import Spender
from imbue.minds_evals.data_types import SpenderCost
from imbue.minds_evals.data_types import TokenSnapshot
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.data_types import is_model_observable_on_lane
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.pricing import CacheWriteTtl
from imbue.minds_evals.pricing import PriceMap
from imbue.minds_evals.pricing import cache_write_ttl_for_harness
from imbue.minds_evals.pricing import price_tokens
from imbue.minds_evals.pricing import resolve_price_entry
from imbue.minds_evals.reporting import AGENT_SPENDERS
from imbue.minds_evals.reporting import HARNESS_SPENDERS
from imbue.minds_evals.reporting import SHORT_SHA_LENGTH
from imbue.minds_evals.reporting import SPEND_LEGEND
from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import format_pricing_line
from imbue.minds_evals.reporting import format_spend_cell
from imbue.minds_evals.reporting import format_spend_totals_line
from imbue.minds_evals.reporting import is_spend_qualified
from imbue.minds_evals.reporting import select_spend
from imbue.minds_evals.reporting import write_reports
from imbue.minds_evals.trial_layout import TrialLayout
from imbue.minds_evals.trial_layout import load_json_object
from imbue.minds_evals.trial_layout import load_optional_json_object
from imbue.minds_evals.trial_layout import resolve_trial_layout

# The dimension whose criteria zero the reward when any of them fails: the transcript parses, the
# agent engaged, every turn completed, the run did not time out.
GATES_DIMENSION: Final[str] = "gates"
# rewardkit tags each reward it emits with how it was produced: "llm" and "agent" for its two judge
# kinds, "programmatic" for the .py criteria, whose pass/fail guards are gated elsewhere. Both judge
# kinds are collected, so a case that grows an agent judge does not quietly stop being reported.
_JUDGE_REWARD_KINDS: Final[frozenset[str]] = frozenset({"llm", "agent"})

# What an errored evidence entry carrying no id of its own is called in the report. Never empty:
# both readers join these ids and render an empty join as "none", so an empty id would print a trial
# that failed for unmeasured evidence as one with none -- and being falsy is worse still, since a
# lone empty id would let the trial pass.
_UNNAMED_ENTRY_ID: Final[str] = "(unnamed entry)"

# What the driver writes into state.json when the conversation ran to the end.
_FINISHED_TEST_STATE: Final[str] = "finished"

# How the totals line names each half of a run's spend. The markdown table has room for the word.
_AGENT_SPEND_LABEL: Final[str] = "agent spend"
_HARNESS_SPEND_LABEL: Final[str] = "harness spend"


@pure
def _step_qualified(step_name: str, name: str) -> str:
    """Something a step produced, named with the step it came from. Unqualified for a flat trial,
    which ran no step to name."""
    return name if not step_name else "{}/{}".format(step_name, name)


# _reward_dicts, _criteria and is_gates_dimension_passed below are mirrored by _reward_dicts,
# _criteria and _gates_all_passed in templates/tests/verifier/finalize.py, which decides the same gate
# verdict inside the verifier container. They cannot be shared: that file runs on stdlib and
# rewardkit alone, with no imbue package. Keep the two in step. Two helpers are deliberately local to
# one side: criterion_value below, because this side must always reach a verdict where finalize.py is
# entitled to raise on a malformed file; and finalize.py's _is_gates_dimension_scored, because only
# that side decides whether a trial is graded at all.
@pure
def _reward_dicts(dimension: Any) -> list[dict[str, Any]]:
    """The per-reward detail dicts for one dimension of reward-details.json.

    rewardkit emits a single dict when a dimension directory yields one reward and a list when it
    yields several (a judge .toml alongside programmatic .py criteria), so both shapes occur.
    """
    if isinstance(dimension, dict):
        return [dimension]
    if isinstance(dimension, list):
        return [entry for entry in dimension if isinstance(entry, dict)]
    return []


@pure
def _criteria(reward_dict: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_criteria = reward_dict.get("criteria")
    if not isinstance(raw_criteria, list):
        return []
    return [entry for entry in raw_criteria if isinstance(entry, dict)]


@pure
def criteria_of_dimension(reward_details: Mapping[str, Any], dimension_name: str) -> list[dict[str, Any]]:
    """Every criterion of one dimension of reward-details.json, across the rewards it emitted."""
    return [
        criterion
        for reward_dict in _reward_dicts(reward_details.get(dimension_name))
        for criterion in _criteria(reward_dict)
    ]


@pure
def criterion_value(criterion: Mapping[str, Any]) -> float:
    """A criterion's normalized 0-1 score, or 0.0 when it carries none this can read.

    reward-details.json is read without a schema, so an unreadable value has to mean something. Zero
    is the safe reading for both callers: it fails a gate, which is the same verdict as a gate that
    was never scored, and it reports a judge criterion at the bottom of its scale rather than hiding
    it. A boolean is excluded before the numeric check because `isinstance(True, int)` holds.
    """
    value = criterion.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


@pure
def is_gates_dimension_passed(reward_details: Mapping[str, Any] | None) -> bool:
    """Whether every structural gate criterion scored above zero.

    A dimension with no criteria at all counts as not passed: it means the verifier never scored the
    gates, which is not the same claim as the gates holding. The verifier errors such a trial rather
    than grading it, so what this side is reading there is a trial the run already fails on its
    harbor status.
    """
    if reward_details is None:
        return False
    reward_dicts = _reward_dicts(reward_details.get(GATES_DIMENSION))
    is_any_criterion_seen = False
    for reward_dict in reward_dicts:
        for criterion in _criteria(reward_dict):
            is_any_criterion_seen = True
            if criterion_value(criterion) <= 0.0:
                return False
    return is_any_criterion_seen


def _read_step_reward_details(layout: TrialLayout) -> tuple[tuple[str, dict[str, Any] | None], ...]:
    """Each step's rewardkit breakdown beside the name of the step that produced it, in step order.

    Read once for both readings the report takes of it, the gate verdict and the judge scores, so the
    two cannot come from different versions of the same file.
    """
    return tuple((step.step_name, load_optional_json_object(step.reward_details_path)) for step in layout.steps)


@pure
def _is_every_step_gated_open(step_details: Sequence[tuple[str, Mapping[str, Any] | None]]) -> bool:
    """Whether the structural gates held on every step the trial ran.

    Every step is graded by its own verifier against its own expectations, and a gate that failed on
    any of them is a structural failure of the whole trial: that step's conversation did not run as
    the case configured it, whatever a later step went on to score. The trial's reward is a separate
    question, answered by harbor's own selection in result.json rather than re-derived here -- which
    step that is depends on the task's reward strategy, and the task config is not in the job
    directory.
    """
    return bool(step_details) and all(is_gates_dimension_passed(details) for _, details in step_details)


def collect_judge_scores(reward_details: Mapping[str, Any] | None) -> tuple[JudgeScore, ...]:
    """Every likert criterion the judges scored, across every dimension. Reported, never gated.

    A criterion whose likert answer cannot be read is the one thing that cannot be reported, since
    there is no number to report; it is dropped and logged. Silence would be worse than the gap: the
    report is the only place a judge score exists, so an empty judge column reads as a case with no
    judges rather than as one whose answers went unread.
    """
    if reward_details is None:
        return ()
    scores: list[JudgeScore] = []
    for dimension_name, dimension in sorted(reward_details.items()):
        for reward_dict in _reward_dicts(dimension):
            if reward_dict.get("kind") not in _JUDGE_REWARD_KINDS:
                continue
            for criterion in _criteria(reward_dict):
                raw_score = criterion.get("raw")
                if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
                    logger.warning(
                        "Dropping the judge criterion {}.{}: its likert answer {!r} is not a number",
                        dimension_name,
                        criterion.get("name"),
                        raw_score,
                    )
                    continue
                scores.append(
                    JudgeScore(
                        dimension=dimension_name,
                        criterion=str(criterion.get("name") or ""),
                        normalized_score=criterion_value(criterion),
                        raw_score=float(raw_score),
                    )
                )
    return tuple(scores)


def _collect_step_judge_scores(
    step_details: Sequence[tuple[str, Mapping[str, Any] | None]],
) -> tuple[JudgeScore, ...]:
    """Every likert criterion the judges scored on the trial, across every step it ran.

    A stepped case is scored once per step against that step's own expectations, so each score
    carries the step it was given on. The step goes on the criterion rather than on the dimension,
    which is rewardkit's own name for the directory that scored it: both reports print the criterion,
    and both key a judge column on the pair, so this is the one place that makes three answers to the
    same question three answers rather than one column and one cell of repeated numbers.
    """
    return tuple(
        score.model_copy_update(to_update(score.field_ref().criterion, _step_qualified(step_name, score.criterion)))
        for step_name, details in step_details
        for score in collect_judge_scores(details)
    )


@pure
def _token_count(raw_tokens: Mapping[str, Any], key: str) -> int:
    """One bucket of a `tokens` block, zero where it names none. A boolean is excluded before the
    numeric check because `isinstance(True, int)` holds."""
    count = raw_tokens.get(key)
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


@pure
def _token_snapshot(raw_tokens: Any) -> TokenSnapshot:
    """A `tokens` block of usage.json as the buckets pricing needs, read without a schema like every
    other artifact here: a counter the file does not name is zero, because a writer that recorded no
    such bucket recorded no tokens in it. That a spender recorded nothing at all is said by its block
    being absent, never by the contents of one."""
    if not isinstance(raw_tokens, Mapping):
        return TokenSnapshot()
    return TokenSnapshot(
        input=_token_count(raw_tokens, "input"),
        output=_token_count(raw_tokens, "output"),
        cache_read=_token_count(raw_tokens, "cache_read"),
        cache_creation=_token_count(raw_tokens, "cache_write"),
    )


@pure
def _standard_portion(tokens: TokenSnapshot, fast_tokens: TokenSnapshot) -> TokenSnapshot:
    """The part of one model's tokens that was not served in fast mode.

    The fast counts are a subset of the totals rather than an addition to them, so what is left of the
    totals is what ran standard. Floored at zero: a record whose subset exceeds its own totals is
    contradicting itself, and a negative bucket would show up as a discount.
    """
    return TokenSnapshot(
        input=max((tokens.input or 0) - (fast_tokens.input or 0), 0),
        output=max((tokens.output or 0) - (fast_tokens.output or 0), 0),
        cache_read=max((tokens.cache_read or 0) - (fast_tokens.cache_read or 0), 0),
        cache_creation=max((tokens.cache_creation or 0) - (fast_tokens.cache_creation or 0), 0),
    )


@pure
def _per_model_cost(
    row: Mapping[str, Any], price_map: PriceMap, cache_write_ttl: CacheWriteTtl, requested_model: str
) -> float | None:
    """What one per-model row of the workspace block cost, or None where nothing prices its model.

    The two speed tiers are priced apart, each at its own rate: a row records the fast portion as a
    subset of its totals, so what is left of them is the standard portion. A model that served
    fast-mode traffic but cannot serve the tier prices nothing at all, rather than at the standard rate
    that would halve a real bill.

    `requested_model` is the catalog id the trial asked the chat to run on, which is what prices a row
    whose model a harness reported without the gateway that billed it.
    """
    entry = resolve_price_entry(str(row.get("model") or ""), price_map, catalog_id=requested_model)
    if entry is None:
        return None
    fast_tokens = _token_snapshot(row.get("fast_tokens"))
    standard_cost_usd = price_tokens(
        entry,
        _standard_portion(_token_snapshot(row.get("tokens")), fast_tokens),
        is_fast_mode=False,
        cache_write_ttl=cache_write_ttl,
    )
    fast_cost_usd = (
        price_tokens(entry, fast_tokens, is_fast_mode=True, cache_write_ttl=cache_write_ttl)
        if fast_tokens.is_any_token_recorded
        else 0.0
    )
    if standard_cost_usd is None or fast_cost_usd is None:
        return None
    return standard_cost_usd + fast_cost_usd


@pure
def _workspace_spend(
    block: Mapping[str, Any], price_map: PriceMap, cache_write_ttl: CacheWriteTtl, requested_model: str
) -> SpenderCost:
    """What the workspace agent under test spent, priced from the tokens it recorded per model.

    Unknown is never partial: one model nothing can price leaves the rest a sum that is not what the
    trial cost, so the whole figure gives way. A block with no per-model rows at all prices nothing
    either -- a trial that recorded no traffic did not cost zero, it recorded nothing to price.

    Both flags read false wherever the block does not say plainly, which every file written before
    they existed is. That is the safe reading in each case: an unflagged figure claims to hold all
    the traffic and to be priced at the rate it was billed at, and only the file itself can say so.
    """
    raw_rows = block.get("per_model")
    rows = [row for row in raw_rows if isinstance(row, Mapping)] if isinstance(raw_rows, list) else []
    cost_by_model = [
        (str(row.get("model") or ""), _per_model_cost(row, price_map, cache_write_ttl, requested_model))
        for row in rows
    ]
    is_any_model_unpriced = any(cost_usd is None for _, cost_usd in cost_by_model)
    # Only a model the row actually named, so a row that named none reads as the bare refusal to
    # price rather than as a naming with nothing in it.
    unpriced_models = tuple(model for model, cost_usd in cost_by_model if cost_usd is None and model)
    return SpenderCost(
        spender=Spender.WORKSPACE_AGENT,
        cost_usd=None
        if is_any_model_unpriced or not cost_by_model
        else sum(cost_usd or 0.0 for _, cost_usd in cost_by_model),
        is_complete=block.get("is_cost_complete") is True,
        is_rate_certain=block.get("is_cost_rate_certain") is True,
        unpriced_models=unpriced_models,
    )


@pure
def _unanswered_call_count(block: Mapping[str, Any]) -> int:
    """Calls a harness block reports that came back with nothing usable, under whichever name its
    spender records them -- the decider's fallbacks, the flow agent's failed calls.

    A call that raised reports zero tokens even where the provider billed it (a read timeout lands
    after the answer was generated), and a call that answered unusably was billed and is in the
    total. The file cannot tell the two apart, so any of them makes the figure a floor.
    """
    counts = (block.get("fallback_count"), block.get("failed_call_count"))
    return sum(count for count in counts if isinstance(count, int) and not isinstance(count, bool))


@pure
def _harness_spend(spender: Spender, block: Mapping[str, Any], price_map: PriceMap) -> SpenderCost:
    """What one of the harness's own models -- the decider, the UI-flow verification agent -- spent.

    Both are direct API calls the host makes itself, on one model at one rate and with no prompt cache
    between them, so there is no speed tier and no cache TTL to be uncertain about; the only thing
    that can go unknown is the price of the model, which is what naming an unpriced model says. A
    block that records no tokens at all prices nothing rather than zero, because the file makes no
    claim this side can turn into a figure. Traffic can still go missing: a call that never came back
    was billed all the same and is counted, not measured.
    """
    model = str(block.get("model") or "")
    entry = resolve_price_entry(model, price_map)
    raw_tokens = block.get("tokens")
    cost_usd = (
        None
        if entry is None or not isinstance(raw_tokens, Mapping)
        # Neither caller caches, so the TTL never reaches a rate; the base one is what asking for no
        # cache at all is billed at.
        else price_tokens(
            entry, _token_snapshot(raw_tokens), is_fast_mode=False, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES
        )
    )
    return SpenderCost(
        spender=spender,
        cost_usd=cost_usd,
        is_complete=_unanswered_call_count(block) == 0,
        is_rate_certain=True,
        unpriced_models=(model,) if entry is None and model else (),
    )


@pure
def collect_spend(
    usage: Mapping[str, Any] | None,
    price_map: PriceMap,
    cache_write_ttl: CacheWriteTtl,
    requested_model: str = "",
) -> tuple[SpenderCost, ...]:
    """Every spender the trial's usage.json accounts for, priced, in the order the `Spender` enum
    names them.

    `requested_model` is the catalog id the trial asked the chat to run on, empty where it asked for
    none. Only the workspace agent is priced against it: the two harness blocks name the models the
    host called directly, with no gateway between. Every rate comes from `price_map`, so one run's
    figures are all priced from the one table.

    Read without a schema, the way state.json is: this file is written by whichever driver version
    produced the trial and it grows blocks over time, so refusing an older shape would make an older
    trial unreadable rather than merely less informative. A block that is absent, `null` or empty
    yields no entry at all: `null` is the driver saying that spender never ran, an absent key is a
    file written before it could, and neither is a claim that it cost nothing.

    Matched rather than split in two, so that a spender added to the enum has to be placed: reading
    an unplaced one as harness spend would claim the completeness only a host-side call can claim.
    """
    if usage is None:
        return ()
    spend: list[SpenderCost] = []
    for spender in Spender:
        block = usage.get(spender.value)
        if not isinstance(block, Mapping) or not block:
            continue
        match spender:
            case Spender.WORKSPACE_AGENT:
                spend.append(_workspace_spend(block, price_map, cache_write_ttl, requested_model))
            case Spender.DECIDER | Spender.VERIFIER_AGENT:
                spend.append(_harness_spend(spender, block, price_map))
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(spend)


def read_trial_spend(
    layout: TrialLayout, price_map: PriceMap, cache_write_ttl: CacheWriteTtl, requested_model: str
) -> tuple[SpenderCost, ...]:
    """What one trial spent, per spender; empty when it wrote no usage account at all.

    The account is cumulative across a stepped trial's steps, so the tokens are the last step's.

    Raises JobReadError for a file that is there but unreadable, like every other artifact here.
    """
    return collect_spend(
        None if layout.usage_path is None else load_optional_json_object(layout.usage_path),
        price_map,
        cache_write_ttl,
        requested_model,
    )


def collect_error_entry_ids(manifest: Mapping[str, Any] | None, manifest_path: Path) -> tuple[str, ...]:
    """The evidence entries the harness could not measure.

    A missing manifest yields nothing rather than an error: a trial that never reached a workspace
    writes none, and its structural gates already say the run went wrong. A manifest that is there
    but carries no readable entries yields nothing too -- this file is read without a schema, because
    a job directory can have been produced by an older driver, and refusing it would make an older
    trial unreadable rather than merely less informative -- but it says so, because the silent
    reading of it is "nothing went unmeasured", which is the one verdict this gate exists to deny.
    """
    if manifest is None:
        return ()
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        logger.warning(
            "{} carries no readable 'entries' list, so nothing can be said about unmeasured evidence",
            manifest_path,
        )
        return ()
    return tuple(
        str(entry.get("entry_id") or _UNNAMED_ENTRY_ID)
        for entry in raw_entries
        if isinstance(entry, dict) and entry.get("status") == CheckStatus.ERROR.value
    )


def _collect_trial_error_entry_ids(layout: TrialLayout) -> tuple[str, ...]:
    """Every evidence entry the harness could not measure, across every step of the trial.

    Each step collects its own evidence, so every step's manifest is read rather than only the last
    one's: a step that went unmeasured is a step the trial cannot be judged on, whatever the steps
    after it managed to measure. The ids carry their step, so the report says where to look.
    """
    entry_ids: list[str] = []
    for step in layout.steps:
        manifest = load_optional_json_object(step.evidence_manifest_path)
        entry_ids.extend(
            _step_qualified(step.step_name, entry_id)
            for entry_id in collect_error_entry_ids(manifest, step.evidence_manifest_path)
        )
    return tuple(entry_ids)


@pure
def _declared_step_count(state: Mapping[str, Any] | None) -> int:
    """How many steps the trial's task declared, out of the driver's own record of it. Zero for a
    flat trial, and for a state file written before the count was recorded.

    Harbor's result.json cannot answer this: `MultiStepTrial._run` appends a step result per step it
    STARTS, so a trial that stopped after the second of three records two, and the task config that
    declares the third is not in the job directory at all.
    """
    step_count = state.get("step_count") if state is not None else None
    return step_count if isinstance(step_count, int) and not isinstance(step_count, bool) else 0


@pure
def describe_incompletion(result: TrialResult | None, state: Mapping[str, Any] | None) -> str:
    """Why the trial did not run to the end, or empty when it did.

    Harbor records infrastructure failures as an exception and everything else as a graded trial, so
    a conversation that ran out of time is a normal result here and only the driver's own state file
    tells it apart from a conversation that finished badly.
    """
    if result is None:
        return "harbor wrote no result.json (the trial never finished)"
    if result.exception_info is not None:
        return "{}: {}".format(
            result.exception_info.exception_type, result.exception_info.exception_message.strip()[:200]
        )
    for step_result in result.step_results or ():
        if step_result.exception_info is not None:
            return "step {} raised {}: {}".format(
                step_result.step_name,
                step_result.exception_info.exception_type,
                step_result.exception_info.exception_message.strip()[:200],
            )
    if state is None:
        return "the driver wrote no state.json (the trial never got past setup)"
    if state.get("test_state") != _FINISHED_TEST_STATE:
        return "the conversation ended in state {!r}".format(state.get("test_state"))
    # A stepped trial that stopped early says so nowhere else. Harbor stops iterating the steps the
    # moment one misses its reward floor, and records that step as an ordinary graded one: it ran its
    # conversation to the end, so the state file it wrote -- the last one there is -- reads exactly
    # like the final step of a trial that ran them all. Only the steps after it are missing, which is
    # what the count the task declared is compared against.
    declared_step_count = _declared_step_count(state)
    completed_step_count = len(result.step_results or ())
    if declared_step_count and completed_step_count < declared_step_count:
        return "only {} of the task's {} steps ran (a step below its reward floor stops the rest)".format(
            completed_step_count, declared_step_count
        )
    return ""


@pure
def harness_config_block(state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The harness settings the trial was asked to run on, nested inside the arm block its state
    file records the whole treatment in, or an empty block when the state file says nothing about
    them -- every trial written before arms existed, and any trial that died before the sign-in."""
    arm = (state or {}).get("arm")
    harness_config = arm.get("harness_config") if isinstance(arm, Mapping) else None
    return harness_config if isinstance(harness_config, Mapping) else {}


@pure
def _model_confirmation(harness_config: Mapping[str, Any]) -> bool | None:
    """Whether the trial's transcript confirmed it ran on the model its harness config asked for.

    None wherever the record does not say plainly: it is written as null by a driver that could not
    tell, and is absent altogether from a state file written before arms existed.
    """
    is_confirmed = harness_config.get("is_model_confirmed")
    return is_confirmed if isinstance(is_confirmed, bool) else None


@pure
def _describe_wrong_model(harness_config: Mapping[str, Any]) -> str:
    """Why the trial's model is a failure, or empty when it is not one.

    Only a config that asked for a model is judged on this, and only a plain false is a failure:
    null is what the driver writes wherever it cannot tell (no transcript was captured, or the
    catalog id has no known reported name), and that is silence rather than evidence of a wrong
    model.
    """
    requested_model = str(harness_config.get("model") or "")
    if not requested_model or _model_confirmation(harness_config) is not False:
        return ""
    raw_observed = harness_config.get("observed_models")
    observed = [str(model) for model in raw_observed] if isinstance(raw_observed, list) else []
    return "the run asked for {} but the trial answered on {}".format(
        requested_model, ", ".join(observed) or "no model the record names"
    )


@pure
def _trial_reward(result: TrialResult | None) -> float | None:
    if result is None or result.verifier_result is None or result.verifier_result.rewards is None:
        return None
    reward = result.verifier_result.rewards.get("reward")
    return None if reward is None else float(reward)


def load_trial_result(result_path: Path) -> TrialResult | None:
    """Harbor's own record of the trial, or None when it never wrote one.

    Raises JobReadError for a file that is there but is not a TrialResult -- truncated by the crash
    the check is diagnosing, or written by a harbor whose schema moved. That is a job that cannot be
    read, which is a different claim from a run that failed.
    """
    if not result_path.is_file():
        return None
    try:
        return TrialResult.model_validate(load_json_object(result_path))
    except ValidationError as exc:
        raise JobReadError("{} is not a harbor trial result: {}".format(result_path, exc)) from exc


def read_trial_state(layout: TrialLayout) -> dict[str, Any] | None:
    """The driver's own state record for one trial, or None when it never wrote one.

    The record is cumulative across a stepped trial's steps, so it is the last step's that describes
    the trial.

    Read without a schema on purpose: state.json is written by whichever driver version produced the
    trial, and it grows keys over time. Raises JobReadError for a file that is there but unreadable.
    """
    return None if layout.state_path is None else load_optional_json_object(layout.state_path)


def _read_trial_check(trial_dir: Path, price_map: PriceMap) -> TrialCheck:
    """Everything the run gate needs from one trial directory, tolerating each artifact's absence.

    Where each artifact is comes from the layout resolver, so a stepped trial is read from its steps
    and a flat one from the trial-root paths.
    """
    layout = resolve_trial_layout(trial_dir)
    result = load_trial_result(layout.result_path)
    state = read_trial_state(layout)
    step_details = _read_step_reward_details(layout)
    incompletion_reason = describe_incompletion(result, state)
    state_values = state or {}
    harness_config = harness_config_block(state)
    return TrialCheck(
        trial_name=trial_dir.name,
        case_id=str(state_values.get("case_name") or ""),
        is_completed=not incompletion_reason,
        incompletion_reason=incompletion_reason,
        step_count=_declared_step_count(state),
        completed_step_count=len(layout.step_names),
        is_gates_passed=_is_every_step_gated_open(step_details),
        error_entry_ids=_collect_trial_error_entry_ids(layout),
        reward=_trial_reward(result),
        judge_scores=_collect_step_judge_scores(step_details),
        # Two things about a price the trial's own record decides: which prompt-cache TTL its cache
        # writes are billed at follows the harness it ran, and the model it asked for is what prices a
        # row whose model the harness reported without the gateway that billed it.
        spend=read_trial_spend(
            layout,
            price_map,
            cache_write_ttl_for_harness(str(harness_config.get("harness") or "")),
            str(harness_config.get("model") or ""),
        ),
        lane=str(harness_config.get("lane") or ""),
        requested_model=str(harness_config.get("model") or ""),
        is_model_confirmed=_model_confirmation(harness_config),
        wrong_model_reason=_describe_wrong_model(harness_config),
        modal_environment_name=str(state_values.get("modal_environment_name") or ""),
        mngr_sha=str(state_values.get("mngr_sha") or ""),
        dwt_sha=str(state_values.get("dwt_sha") or ""),
    )


def list_trial_dirs(job_dir: Path) -> list[Path]:
    """Every trial directory in a job directory, in name order.

    Harbor keeps only files at the job root, so a subdirectory is a trial -- including one that never
    wrote a result, which is exactly the case a pass/fail gate must not skip over. The exception is a
    dot-directory: harbor caches a regrade's downloaded source under `<job dir>/.sources/<uuid>`, and
    reading that as a trial would fail a run over an artifact of having regraded it.
    """
    return sorted(entry for entry in job_dir.iterdir() if entry.is_dir() and not entry.name.startswith("."))


def check_job_directory(job_dir: Path, price_map: PriceMap) -> RunCheck:
    """Read every trial in a finished job directory and decide whether the run passed.

    Every figure in the result is priced from `price_map`, and the report names that map: one report
    is one map, whatever a later lookup of the same model would have answered.

    Raises JobReadError if the directory holds no trials at all -- an empty job says the run never
    started, which must not be reported as a run that passed.
    """
    if not job_dir.is_dir():
        raise JobReadError("{} is not a job directory".format(job_dir))
    trial_dirs = list_trial_dirs(job_dir)
    if not trial_dirs:
        raise JobReadError("{} holds no trial directories".format(job_dir))
    trials = tuple(_read_trial_check(trial_dir, price_map) for trial_dir in trial_dirs)
    return RunCheck(job_name=job_dir.name, trials=trials, pricing=price_map.pricing_source)


@pure
def _format_judge_scores(judge_scores: Sequence[JudgeScore]) -> str:
    if not judge_scores:
        return "-"
    return ", ".join("{} {:.1f}".format(score.criterion, score.raw_score) for score in judge_scores)


@pure
def _format_marker(is_ok: bool) -> str:
    return "pass" if is_ok else "FAIL"


@pure
def _format_completed_cell(trial: TrialCheck) -> str:
    """The completion column: the verdict, and for a stepped trial how many steps stand behind it.

    One row stands for the whole trial, so a three-step pass and the same case passing one step and
    stopping would otherwise read alike.
    """
    if not trial.is_completed:
        return as_table_cell(trial.incompletion_reason)
    if not trial.completed_step_count:
        return _format_marker(True)
    return "{} ({} {})".format(
        _format_marker(True), trial.completed_step_count, "step" if trial.completed_step_count == 1 else "steps"
    )


@pure
def _format_arm_cell(trial: TrialCheck) -> str:
    """The harness half of the trial's arm as one cell: what it asked to run on, and what the
    transcript said about it. The pinned pair that completes the arm has columns of its own beside
    this one.

    The failure takes the cell whenever there is one, the way the completion column carries its own
    reason, so a run that answered on the wrong model says so where it is read.
    """
    if trial.wrong_model_reason:
        return trial.wrong_model_reason
    if not trial.lane and not trial.requested_model:
        return "-"
    if not trial.requested_model:
        # The default harness config requests no model at all, so there is nothing for the
        # transcript to confirm and no name to print but the workspace's own default.
        return "{} default".format(trial.lane or "-")
    if not is_model_observable_on_lane(trial.lane):
        # Never "unconfirmed": nothing confirmed it because the harness names no model at all, which
        # is a different thing to tell a reader than a claude trial whose transcript went missing.
        return "{} {} not observable".format(trial.lane, trial.requested_model)
    return "{} {} {}".format(
        trial.lane or "-",
        trial.requested_model,
        "confirmed" if trial.is_model_confirmed else "unconfirmed",
    )


@pure
def render_summary_markdown(run_check: RunCheck) -> str:
    """The run as a GitHub step-summary table: one row per trial, one verdict line above it, and what
    the run spent -- priced by the map the line beside it names -- above that."""
    # Both halves of every trial's spend, rendered first: the legend under the table is read off the
    # very cells the rows carry, and the totals line above it puts the same rules (`reporting`'s
    # own) to the same records, so the three cannot disagree.
    agent_cells = [format_spend_cell(select_spend(trial.spend, AGENT_SPENDERS)) for trial in run_check.trials]
    harness_cells = [format_spend_cell(select_spend(trial.spend, HARNESS_SPENDERS)) for trial in run_check.trials]
    totals_line = format_spend_totals_line(
        [trial.spend for trial in run_check.trials], _AGENT_SPEND_LABEL, _HARNESS_SPEND_LABEL
    )
    # Which map priced those figures belongs with them: a run re-checked after a price change reports
    # different money from the same tokens.
    spend_lines = [line for line in (totals_line, format_pricing_line(run_check.pricing)) if line]
    header_lines = [
        "## minds-evals: {} -- {}".format(run_check.job_name, _format_marker(run_check.is_passed)),
        "",
        *((*spend_lines, "") if totals_line else ()),
        "| trial | case | arm | completed | gates | errored evidence | reward | agent cost | harness cost"
        " | judge scores | modal env | mngr | dwt |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    trial_lines = [
        "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | `{}` | `{}` | `{}` |".format(
            as_table_cell(trial.trial_name),
            as_table_cell(trial.case_id) or "-",
            as_table_cell(_format_arm_cell(trial)),
            _format_completed_cell(trial),
            _format_marker(trial.is_gates_passed),
            as_table_cell(", ".join(trial.error_entry_ids)) or "none",
            "-" if trial.reward is None else "{:.4f}".format(trial.reward),
            as_table_cell(agent_cell),
            as_table_cell(harness_cell),
            as_table_cell(_format_judge_scores(trial.judge_scores)),
            as_table_cell(trial.modal_environment_name) or "-",
            as_table_cell(trial.mngr_sha[:SHORT_SHA_LENGTH]) or "-",
            as_table_cell(trial.dwt_sha[:SHORT_SHA_LENGTH]) or "-",
        )
        for trial, agent_cell, harness_cell in zip(run_check.trials, agent_cells, harness_cells, strict=True)
    ]
    legend_lines = ["", SPEND_LEGEND] if is_spend_qualified([*agent_cells, *harness_cells]) else []
    return "\n".join([*header_lines, *trial_lines, *legend_lines, ""])


def write_run_check_reports(run_check: RunCheck, summary_md_path: Path | None, summary_json_path: Path | None) -> None:
    write_reports(
        [
            (summary_md_path, render_summary_markdown(run_check)),
            (summary_json_path, run_check.model_dump_json(indent=2) + "\n"),
        ]
    )
