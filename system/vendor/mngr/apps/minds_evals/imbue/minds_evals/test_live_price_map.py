from typing import Final

import pytest

from imbue.minds_evals.ci_matrix import CHECKED_IN_HARNESS_CONFIGS_PATH
from imbue.minds_evals.ci_matrix import load_harness_configs
from imbue.minds_evals.pricing import load_litellm_price_map
from imbue.minds_evals.pricing import resolve_price_entry

# The models the claude lane answers on, which no harness config names: the default config requests
# nothing and runs on the product's pinned Opus (`opus[1m]`, which the harness reports as
# `claude-opus-5`), the `haiku` config asks for the catalog id `haiku`, which is the workspace's name
# for the model rather than the price map's, and `claude-opus-4-8` is what the decider runs on and
# what an api-key-lane trial gets by default. `claude-opus-5` is also the one id here that litellm's
# bundled map does not hold, so a run answered by that copy fails this rather than reporting the
# default arm's whole cost as unknown in silence.
_CLAUDE_LANE_MODELS: Final[tuple[str, ...]] = ("claude-opus-5", "claude-opus-4-8", "claude-haiku-4-5")


@pytest.mark.acceptance
def test_litellm_prices_every_model_a_run_reports() -> None:
    """The drift test, and the only test whose result depends on litellm's live map -- which litellm
    fetches over the network when it is imported, falling back to the copy its wheel ships.

    Every figure a report prints depends on that map holding the run's models under exactly these
    keys, and a litellm bump that drops one (or renames it) would otherwise show up as a run whose
    cost quietly went unknown -- which reads like a trial that spent nothing unusual. The pi arms'
    catalog ids are read from the checked-in harness configs, so an arm added there is covered here
    without being named twice.

    No amount is asserted. Upstream edits prices, and a price change is not a failure of this
    project; every rule about what a rate is then worth is measured against the fixture map in
    `pricing_test.py`.
    """
    price_map = load_litellm_price_map()
    # The configs whose model is a provider-qualified tag are the pi arms, whose reported rows are
    # priced under the tag itself; a claude config names a workspace catalog id, which is not a key.
    catalog_ids = tuple(
        entry.model for entry in load_harness_configs(CHECKED_IN_HARNESS_CONFIGS_PATH) if "/" in entry.model
    )
    assert catalog_ids, "the harness configs name no gateway arm, so this test would check nothing"

    unpriced_model_ids = [
        model_id
        for model_id in (*_CLAUDE_LANE_MODELS, *catalog_ids)
        if resolve_price_entry(model_id, price_map) is None
    ]

    assert unpriced_model_ids == []
    # Which map answered is recorded beside the figures, because litellm's version alone does not pin
    # the rates: it serves either its published map or the copy bundled in its wheel.
    assert price_map.pricing_source.source == "litellm"
    assert price_map.pricing_source.map_source in ("local", "remote")
