from pathlib import Path

import pytest

from imbue.minds_evals.data_types import PricingSource
from imbue.minds_evals.data_types import Spender
from imbue.minds_evals.data_types import SpenderCost
from imbue.minds_evals.reporting import AGENT_SPENDERS
from imbue.minds_evals.reporting import HARNESS_SPENDERS
from imbue.minds_evals.reporting import NO_SPEND_MARK
from imbue.minds_evals.reporting import UNPRICED_MARK
from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import format_pricing_line
from imbue.minds_evals.reporting import format_spend_cell
from imbue.minds_evals.reporting import format_spend_total
from imbue.minds_evals.reporting import format_spend_totals_line
from imbue.minds_evals.reporting import is_spend_qualified
from imbue.minds_evals.reporting import total_spend
from imbue.minds_evals.reporting import write_reports


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("a | b", "a \\| b"),
        ("first\nsecond", "first second"),
        ("a|b\nc", "a\\|b c"),
        ("", ""),
    ],
)
def test_as_table_cell_keeps_free_text_inside_its_cell(text: str, expected: str) -> None:
    """A pipe ends the cell and a newline ends the row, so a ref, a config path or an exception
    message carrying either would silently rewrite the table around it."""
    assert as_table_cell(text) == expected


def test_write_reports_creates_the_directory_and_skips_the_reports_not_asked_for(tmp_path: Path) -> None:
    written_path = tmp_path / "nested" / "summary.md"

    write_reports([(written_path, "content\n"), (None, "never written")])

    assert written_path.read_text() == "content\n"
    assert list(tmp_path.iterdir()) == [tmp_path / "nested"]


def _spender_cost(
    spender: Spender,
    cost_usd: float | None,
    *,
    is_complete: bool = True,
    unpriced_models: tuple[str, ...] = (),
) -> SpenderCost:
    return SpenderCost(
        spender=spender,
        cost_usd=cost_usd,
        is_complete=is_complete,
        is_rate_certain=True,
        unpriced_models=unpriced_models,
    )


@pytest.mark.parametrize(
    ("cost_usd", "is_complete", "expected"),
    [
        pytest.param(0.0, True, "$0.00", id="a spender that ran no model"),
        pytest.param(0.003, True, "<$0.01", id="less than the width can show"),
        pytest.param(0.003, False, "<$0.01+", id="less than the width can show, and a floor besides"),
        pytest.param(0.005, True, "$0.01", id="the first figure that rounds up"),
        pytest.param(22.484, True, "$22.48", id="a figure read as it is"),
    ],
)
def test_format_spend_cell_keeps_a_figure_too_small_to_show_apart_from_a_zero(
    cost_usd: float, is_complete: bool, expected: str
) -> None:
    """A spender that ran no model really did cost nothing, and a report saying so is informative. A
    handful of haiku tokens is not nothing, and printing them as `$0.00` would claim it was -- and a
    figure that small is still a floor when the record says the spender spent more than it holds."""
    assert format_spend_cell((_spender_cost(Spender.DECIDER, cost_usd, is_complete=is_complete),)) == expected


def test_format_spend_cell_names_an_unpriced_model_once_however_many_spenders_ran_on_it() -> None:
    """The cell says which models left the spend unpriced, not how many spenders met each one: two
    host-side calls on the same unpriced model is one thing to go and price."""
    entries = (
        _spender_cost(Spender.DECIDER, None, unpriced_models=("kimi-k2.6",)),
        _spender_cost(Spender.VERIFIER_AGENT, None, unpriced_models=("kimi-k2.6", "glm-4.7-flash")),
    )

    assert format_spend_cell(entries) == "? unpriced: kimi-k2.6, glm-4.7-flash"


def test_one_unpriced_spender_withdraws_the_whole_groups_figure() -> None:
    """A group is priced whole or not at all. The decider and the verification agent are summed into
    one cell, so a verifier on a model nothing carries leaves the decider's figure standing for the
    pair -- a partial sum that reads as the whole harness cost and understates the run with nothing
    to say so. Both readings the rule feeds are pinned here, because a cell and the totals line above
    it must not classify the same records differently."""
    mixed = (
        _spender_cost(Spender.DECIDER, 0.25),
        _spender_cost(Spender.VERIFIER_AGENT, None, unpriced_models=("glm-4.7-flash",)),
    )

    total = total_spend([mixed], HARNESS_SPENDERS)

    assert format_spend_cell(mixed) == "? unpriced: glm-4.7-flash"
    assert (total.cost_usd, total.known_trial_count, total.unknown_trial_count) == (0.0, 0, 1)


def test_format_spend_cell_keeps_apart_the_two_ways_a_cell_carries_no_figure() -> None:
    """Nothing recorded and nothing priceable are different claims about a trial. An oracle trial
    recorded no such spender at all; a trial that recorded traffic under no model id priced nothing
    and has nothing to blame for it, so the refusal stands on its own rather than naming a model the
    trial never gave."""
    assert format_spend_cell(()) == NO_SPEND_MARK
    assert format_spend_cell((_spender_cost(Spender.WORKSPACE_AGENT, None),)) == UNPRICED_MARK


def test_format_spend_totals_line_names_only_the_halves_the_run_recorded() -> None:
    """Both halves in the one wording every report uses. A half no trial recorded is left out rather
    than printed as a zero no spender earned, and a run that recorded neither has no line at all --
    which is what keeps a run of oracle trials from carrying a totals line about nothing."""
    both = [(_spender_cost(Spender.WORKSPACE_AGENT, 1.50), _spender_cost(Spender.DECIDER, 0.25))]
    agent_only = [(_spender_cost(Spender.WORKSPACE_AGENT, 1.50),)]

    assert format_spend_totals_line(both, "agent spend", "harness spend") == (
        "agent spend $1.50 over 1 trial; harness spend $0.25 over 1 trial"
    )
    assert format_spend_totals_line(agent_only, "agent spend", "harness spend") == "agent spend $1.50 over 1 trial"
    assert format_spend_totals_line([()], "agent spend", "harness spend") == ""


def test_is_spend_qualified_reads_the_legends_marks_off_the_rendered_cells() -> None:
    """Whether the legend is owed is asked of the very cells the rows carry, not recomputed from the
    records they came from, so a table and the paragraph under it cannot disagree about which marks
    are in it."""
    assert is_spend_qualified(("$1.50", "-")) is False
    assert is_spend_qualified(("$1.50", "$0.25+")) is True
    assert is_spend_qualified(("$1.50", "? unpriced: kimi-k2.6")) is True


def test_total_spend_counts_a_trial_it_cannot_price_without_letting_it_into_the_figure() -> None:
    """What a run's totals line is built from. The trials with a figure are summed and marked with
    whatever qualifies any of them, a trial nothing could price is counted beside them so the sum
    never reads as covering it, and a trial that recorded only the other half is in neither number."""
    spend_by_trial = [
        (_spender_cost(Spender.WORKSPACE_AGENT, 1.50, is_complete=False),),
        (_spender_cost(Spender.WORKSPACE_AGENT, None, unpriced_models=("kimi-k2.6",)),),
        (_spender_cost(Spender.DECIDER, 0.25),),
    ]

    total = total_spend(spend_by_trial, AGENT_SPENDERS)

    assert (total.cost_usd, total.known_trial_count, total.unknown_trial_count) == (1.50, 1, 1)
    assert total.trial_count == 2
    assert total.is_floor is True
    assert format_spend_total("agent spend", total) == "agent spend $1.50+ over 2 trials, 1 unknown"


def test_every_spender_is_reported_under_exactly_one_half_of_a_runs_spend() -> None:
    """The two halves are what every report splits a trial into. A spender in neither would be
    collected, dumped into the summary, and then appear in no cell and in no total."""
    assert AGENT_SPENDERS | HARNESS_SPENDERS == set(Spender)
    assert not AGENT_SPENDERS & HARNESS_SPENDERS


@pytest.mark.parametrize(
    ("pricing", "expected"),
    [
        pytest.param(
            PricingSource(source="litellm", version="1.93.0", map_source="remote"),
            "priced by litellm 1.93.0 (remote price map)",
            id="a map that says where it came from",
        ),
        pytest.param(
            PricingSource(source="litellm", version="1.93.0", map_source=""),
            "priced by litellm 1.93.0",
            id="a map that does not say which copy answered",
        ),
        pytest.param(PricingSource(source="", version="", map_source=""), "", id="a summary written before the block"),
        pytest.param(PricingSource(source="litellm", version="", map_source="remote"), "", id="a half-filled block"),
    ],
)
def test_format_pricing_line_says_nothing_where_the_record_does_not(pricing: PricingSource, expected: str) -> None:
    """The line exists because nothing in a figure shows what priced it. A record that cannot name
    both the library and its version says nothing at all rather than a half-line a reader would have
    to interpret -- which is what `RunCheck`'s default carries, and what `ci_report` meets when it
    reads back a summary written before the figures recorded their source."""
    assert format_pricing_line(pricing) == expected
