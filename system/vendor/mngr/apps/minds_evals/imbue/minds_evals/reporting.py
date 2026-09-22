"""What the reports in this package share.

Three renderers write about the same runs: `check_run`'s per-trial table, `ci_matrix`'s decision
table, and `ci_report`'s Slack message. All three shorten a SHA to the same width, so that two of
them can be read side by side; the two that render markdown tables also escape free text into a cell
and write only the reports they were asked for. The rules live here so they cannot drift apart.

`check_run` and `ci_report` both report cost. How a group of spenders is classified -- priced whole
or not at all, a floor or an exact figure -- lives here so the two cannot disagree about the same
records, and so does the spelling of the figure and of its marks, so that a floor reads the same in
a markdown cell, a Slack grid cell and a totals line.

Cost is reported per spender rather than as one number. The workspace agent's spend is what the eval
measures; the decider's and the verification agent's are what running the eval costs. The two halves
are summed apart, and every figure carries the marks that say how far it can be read -- a floor, or
a model nothing could price.

No figure is a trial's own record of itself: a trial records tokens and a report prices them, so the
run summary also names the price map its figures came from.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import PricingSource
from imbue.minds_evals.data_types import SpendTotal
from imbue.minds_evals.data_types import Spender
from imbue.minds_evals.data_types import SpenderCost

# How much of a SHA a report prints. Long enough to name a commit unambiguously in this repo, short
# enough to read at a glance, and the same width in every report so two of them can be compared.
SHORT_SHA_LENGTH: Final[int] = 12

# The two halves every report splits a trial's spend into. The decider and the verification agent
# are summed together because both are host-side calls on one model at the rate they were billed at:
# adding them is adding one source, not collapsing two.
AGENT_SPENDERS: Final[frozenset[Spender]] = frozenset({Spender.WORKSPACE_AGENT})
HARNESS_SPENDERS: Final[frozenset[Spender]] = frozenset({Spender.DECIDER, Spender.VERIFIER_AGENT})

# What a spend cell says where a trial recorded no such spender at all -- an oracle trial, or one
# written before usage.json existed.
NO_SPEND_MARK: Final[str] = "-"
# What follows a figure that is a lower bound, and what stands in place of one that has no price.
FLOOR_MARK: Final[str] = "+"
UNPRICED_MARK: Final[str] = "?"

# What a figure reads as where it would round to nothing at the width these reports print. A
# spender that ran no model is a real $0.00, so the two must not be spelled the same way.
SUB_CENT_FIGURE: Final[str] = "<$0.01"

# Said once under a table that carries either mark. Both marks are about how a figure was arrived
# at, which nothing in the number itself can show.
SPEND_LEGEND: Final[str] = (
    "`{}` marks a floor: traffic the total does not hold -- delegated calls, or a harness call that"
    " came back with nothing and may have been billed anyway -- or a speed tier nobody observed, so"
    " fast-mode traffic is priced at the standard rate. `{}` is spend nothing could price, naming the"
    " models responsible where the trial recorded them.".format(FLOOR_MARK, UNPRICED_MARK)
)


@pure
def step_qualified(step_name: str, name: str) -> str:
    """Something a step produced, named with the step it came from. Unqualified for a flat trial,
    which ran no step to name.

    Shared because both reports print such names and a reader compares them across the two.
    """
    return name if not step_name else "{}/{}".format(step_name, name)


@pure
def format_pricing_line(pricing: PricingSource) -> str:
    """Which price map a report's figures were computed from, in one line; empty where the record does
    not say, which is every summary written before the figures carried their source.

    Worth a line of its own because nothing in a figure shows it: the trials hold tokens, so the same
    run checked again after a price change reports different money, and the library's version alone
    does not pin the rates either -- litellm refreshes its map over the network and falls back to the
    copy its wheel ships.
    """
    if not pricing.source or not pricing.version:
        return ""
    map_source = " ({} price map)".format(pricing.map_source) if pricing.map_source else ""
    return "priced by {} {}{}".format(pricing.source, pricing.version, map_source)


@pure
def as_table_cell(text: str) -> str:
    """Free text in a markdown table cell. A pipe or a newline in it would end the cell, and every
    cell these reports render carries free text: exception messages carry anything, and case ids,
    trial names, refs, config paths and criterion names are authored strings held to no vocabulary
    the renderer knows."""
    return text.replace("|", "\\|").replace("\n", " ")


@pure
def format_cost_figure(cost_usd: float, is_floor: bool) -> str:
    """One figure in the one spelling every report uses, with the mark that says it is a lower bound.

    Two decimals, because a report is read for what an arm cost rather than audited in it. A figure
    too small to show at that width still has to read as money spent, so it gives way to the bound
    rather than to a zero no spender earned.
    """
    mark = FLOOR_MARK if is_floor else ""
    if 0.0 < cost_usd < 0.005:
        return "{}{}".format(SUB_CENT_FIGURE, mark)
    return "${:.2f}{}".format(cost_usd, mark)


@pure
def select_spend(spend: Sequence[SpenderCost], spenders: AbstractSet[Spender]) -> tuple[SpenderCost, ...]:
    return tuple(entry for entry in spend if entry.spender in spenders)


@pure
def unpriced_model_names(entries: Sequence[SpenderCost]) -> tuple[str, ...]:
    """The models that left this group of spenders with no figure, in first-seen order and named
    once each: two spenders that both ran on the same unpriced model say so once."""
    names: list[str] = []
    for entry in entries:
        for model in entry.unpriced_models:
            if model not in names:
                names.append(model)
    return tuple(names)


@pure
def group_cost(entries: Sequence[SpenderCost]) -> float | None:
    """What a group of spenders cost together, or None where no figure can stand for the group.

    Priced whole or not at all: one spender whose model carries no price leaves the rest a partial
    sum, which is not what the group cost and must not be printed as if it were. Shared with the
    totals so that a cell and the line above it cannot classify the same records differently.
    """
    costs = [entry.cost_usd for entry in entries]
    if any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)


@pure
def is_group_floor(entries: Sequence[SpenderCost]) -> bool:
    """Whether a group's figure holds less than the group spent -- traffic outside the total, or a
    rate nobody observed -- which is what the floor mark says about it."""
    return any(not entry.is_complete or not entry.is_rate_certain for entry in entries)


@pure
def format_spend_cell(entries: Sequence[SpenderCost]) -> str:
    """One group of spenders' cost as a single cell.

    Three readings that must not be confused with one another: nothing was recorded, the spend
    cannot be priced at all (one of its models is not in the table, and a partial sum would
    understate it), or a figure -- marked as a floor where any spender in it reports traffic the
    total does not hold or a rate nobody observed.
    """
    if not entries:
        return NO_SPEND_MARK
    cost_usd = group_cost(entries)
    if cost_usd is None:
        unpriced = unpriced_model_names([entry for entry in entries if entry.cost_usd is None])
        if not unpriced:
            return UNPRICED_MARK
        return "{} unpriced: {}".format(UNPRICED_MARK, ", ".join(unpriced))
    return format_cost_figure(cost_usd, is_group_floor(entries))


@pure
def total_spend(spend_by_trial: Sequence[Sequence[SpenderCost]], spenders: AbstractSet[Spender]) -> SpendTotal:
    """One half of a run's spend, summed over the trials that recorded it.

    A trial is counted whole or not at all, the way its own cell reads: one carrying an unpriced
    model contributes no figure, because the part of it that could be priced is not its cost. A
    trial that recorded nothing for these spenders is not in the count either -- an oracle trial
    must not make a run's spend look spread over more trials than paid for it.
    """
    total_usd = 0.0
    known_trial_count = 0
    unknown_trial_count = 0
    is_floor = False
    for spend in spend_by_trial:
        entries = select_spend(spend, spenders)
        if not entries:
            continue
        cost_usd = group_cost(entries)
        if cost_usd is None:
            unknown_trial_count += 1
            continue
        known_trial_count += 1
        total_usd += cost_usd
        # Only a trial that contributed a figure can qualify the sum: an unknown trial is named by
        # the count beside it instead.
        is_floor = is_floor or is_group_floor(entries)
    return SpendTotal(
        cost_usd=total_usd,
        known_trial_count=known_trial_count,
        unknown_trial_count=unknown_trial_count,
        is_floor=is_floor,
    )


@pure
def format_spend_total(label: str, total: SpendTotal) -> str:
    """One half of a run's spend in words, or nothing at all when no trial recorded that half.

    The trial count rides along because the sum means nothing without it, and the unknown count is
    named separately rather than folded in: those trials spent money this figure does not hold.
    """
    if not total.trial_count:
        return ""
    figure = "unknown" if not total.known_trial_count else format_cost_figure(total.cost_usd, total.is_floor)
    trials = "{} {}".format(total.trial_count, "trial" if total.trial_count == 1 else "trials")
    unknown = (
        ""
        if not total.unknown_trial_count or not total.known_trial_count
        else ", {} unknown".format(total.unknown_trial_count)
    )
    return "{} {} over {}{}".format(label, figure, trials, unknown)


@pure
def format_spend_totals_line(
    spend_by_trial: Sequence[Sequence[SpenderCost]], agent_label: str, harness_label: str
) -> str:
    """Both halves of a run's spend as one line, in the one wording every report uses; empty when
    the run recorded no spend at all. The labels are the caller's because the reports have different
    room for them, never so that the figures can be spelled differently."""
    halves = (
        format_spend_total(agent_label, total_spend(spend_by_trial, AGENT_SPENDERS)),
        format_spend_total(harness_label, total_spend(spend_by_trial, HARNESS_SPENDERS)),
    )
    return "; ".join(half for half in halves if half)


@pure
def is_spend_qualified(cells: Sequence[str]) -> bool:
    """Whether any rendered cell carries a mark the legend has to explain."""
    return any(FLOOR_MARK in cell or UNPRICED_MARK in cell for cell in cells)


def write_reports(reports: Sequence[tuple[Path | None, str]]) -> None:
    """Write each report that was asked for, creating the directory it goes in.

    A None path is a report this run was not asked to write, which is how every command here makes
    its summary outputs optional.
    """
    for path, content in reports:
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        logger.info("Wrote {}", path)
