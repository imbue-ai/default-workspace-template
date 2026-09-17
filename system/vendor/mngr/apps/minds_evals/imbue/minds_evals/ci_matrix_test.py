import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from click.testing import Result

from imbue.imbue_common.primitives import PositiveInt
from imbue.minds_evals import ci_matrix
from imbue.minds_evals.cli import main
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import FrozenPair
from imbue.minds_evals.data_types import HarnessConfigEntry
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import NightlySuite
from imbue.minds_evals.data_types import NightlySuiteArm
from imbue.minds_evals.data_types import OraclePass
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import ResolvedSuite
from imbue.minds_evals.data_types import SuiteArm
from imbue.minds_evals.errors import CiMatrixError
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import read_scheduled_workflow_text

_MNGR_SHA = "a" * 40
_DWT_SHA = "b" * 40
_CONFIG_PATH = "apps/minds_evals/configs/nightly.json"
_OTHER_CONFIG_PATH = "apps/minds_evals/configs/nightly-stepped.json"


def _write_configs(tmp_path: Path, *entries: dict[str, Any], name: str = "harness_configs.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"harness_configs": list(entries)}))
    return path


def _write_suites(tmp_path: Path, *suites: dict[str, Any], name: str = "nightly_suites.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"suites": list(suites)}))
    return path


def _write_eval_config(repo_root: Path, relative_path: str) -> str:
    """An eval config in a checkout of its own, which is what a suite's path is resolved against.
    Its contents are nobody's business here: the suite file is checked for whether the path names a
    file, not for what the file says."""
    config_path = repo_root / relative_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"personas": []}))
    return relative_path


def _suite(*entries: HarnessConfigEntry, config: str = _CONFIG_PATH, attempts: int = 1) -> ResolvedSuite:
    """One suite over the given harness configs, as `resolve_suites` would hand it over."""
    return ResolvedSuite(
        config=config,
        config_slug=ci_matrix.config_slug(config),
        arms=tuple(SuiteArm(entry=entry, attempts=PositiveInt(attempts)) for entry in entries),
    )


def _resolved_pair(pair: str = "main", mngr_sha: str = _MNGR_SHA, dwt_sha: str = _DWT_SHA) -> FrozenPair:
    return FrozenPair(pair=pair, mngr_ref="main", mngr_sha=mngr_sha, dwt_ref="main", dwt_sha=dwt_sha)


def _unresolved_pair(pair: str = "released") -> FrozenPair:
    return FrozenPair(pair=pair, mngr_ref="minds-v9.9.9", mngr_sha="", dwt_ref="minds-v9.9.9", dwt_sha="")


def _write_pairs(tmp_path: Path, *pairs: FrozenPair) -> Path:
    path = tmp_path / "pairs.jsonl"
    path.write_text("".join(pair.model_dump_json() + "\n" for pair in pairs))
    return path


def test_the_checked_in_harness_configs_file_loads_and_every_entry_is_runnable() -> None:
    """The file the scheduled run reads by default. Loading it validates every entry through the
    driver's own kwarg parsing, so this is also the check that nobody can commit a config the
    nightly would refuse hours later on a paid runner."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)

    assert entries
    for entry in entries:
        assert ci_matrix.harness_config_kwargs(entry).key_env


def test_the_checked_in_files_nightly_set_is_exactly_these_configs() -> None:
    """Which arms run every night is a spend decision -- one box per case per cell -- and this file
    is where it is recorded. Marking another config nightly is a real cost increase, so it is a
    deliberate edit here rather than a line nobody reviewed."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)

    nightly = ci_matrix.select_harness_configs(entries, "")

    assert sorted(entry.name for entry in nightly) == [
        "codex-sol-low",
        "codex-terra",
        "default",
        "haiku",
        "pi-gpt-5-mini",
    ]


def test_the_checked_in_suite_file_runs_exactly_these_arms_on_exactly_these_configs() -> None:
    """Which cells run every night is a spend decision -- one box per case per cell -- and this file
    is where it is recorded. Adding a suite, or an arm to one, is a real cost increase, so it is a
    deliberate edit here rather than a line nobody reviewed. The arms of the first suite are not
    listed at all, which is how a suite says "every nightly harness config"."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)
    suites = ci_matrix.load_nightly_suites(ci_matrix.CHECKED_IN_NIGHTLY_SUITES_PATH, ci_matrix.REPO_ROOT)

    resolved = ci_matrix.resolve_suites(suites, entries)

    assert [(suite.config_slug, tuple(arm.entry.name for arm in suite.arms)) for suite in resolved] == [
        ("eval-config-small", ("default", "haiku", "pi-gpt-5-mini", "codex-sol-low", "codex-terra")),
        ("eval-config-time-to-mock", ("opus-standard", "codex-sol-low", "codex-terra")),
    ]
    assert all(int(arm.attempts) == 1 for suite in resolved for arm in suite.arms)


def test_the_checked_in_suite_file_names_an_arm_the_nightly_set_leaves_out() -> None:
    """A suite's named list is not a subset of the nightly set: it runs what that config is worth
    running on, whatever the flag that decides the every-config set says. `opus-standard` is too
    expensive to run against everything and is still worth one suite naming it."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)
    suites = ci_matrix.load_nightly_suites(ci_matrix.CHECKED_IN_NIGHTLY_SUITES_PATH, ci_matrix.REPO_ROOT)

    named = {arm.name for suite in suites for arm in suite.harness_configs}
    nightly = {entry.name for entry in ci_matrix.select_harness_configs(entries, "")}

    assert "opus-standard" in named
    assert "opus-standard" not in nightly


def test_the_checked_in_file_holds_a_default_config_that_requests_no_model() -> None:
    """One arm has to measure the product exactly as it ships. A config that names a model appends
    `--ak model=` and switches the chat off the workspace's own default before turn 1, so only a
    config that requests nothing but its lane leaves the shipped model, effort and speed tier
    alone -- and that arm is named `default`."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)

    default = next(entry for entry in entries if entry.name == "default")
    assert default.is_nightly
    assert default.model == ""
    assert ci_matrix.harbor_args_for(default) == ("--ak", "lane=anthropic", "--ak", "key_env=ANTHROPIC_API_KEY")


def test_every_nightly_config_that_names_a_model_runs_the_standard_speed_tier() -> None:
    """Arms compared night over night have to differ in one axis at a time, and the fast tier is a
    different model behind the same catalog id -- so a named-model arm asks for the standard one."""
    entries = ci_matrix.load_harness_configs(ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH)

    for entry in entries:
        if entry.is_nightly and entry.model:
            assert not ci_matrix.harness_config_kwargs(entry).is_fast, entry.name


def test_load_harness_configs_refuses_a_file_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "harness_configs.json"
    path.write_text("{not json")

    with pytest.raises(CiMatrixError, match="not valid JSON"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_a_file_that_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(CiMatrixError, match="cannot read"):
        ci_matrix.load_harness_configs(tmp_path / "absent.json")


def test_load_harness_configs_refuses_an_unknown_key(tmp_path: Path) -> None:
    """A misspelled kwarg has to be an error rather than a silently ignored one: an entry whose
    `model` was typed `models` would run the default arm under another arm's name."""
    path = _write_configs(tmp_path, {"name": "haiku", "is_nightly": True, "models": "haiku"})

    with pytest.raises(CiMatrixError, match="models"):
        ci_matrix.load_harness_configs(path)


@pytest.mark.parametrize("bad_name", ["Default", "-leading-dash", ".leading-dot", "has_underscore", "has space", ""])
def test_load_harness_configs_refuses_a_name_that_cannot_be_a_job_name(tmp_path: Path, bad_name: str) -> None:
    path = _write_configs(tmp_path, {"name": bad_name, "is_nightly": True})

    with pytest.raises(CiMatrixError, match="lowercase letters"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_accepts_a_name_carrying_a_model_version(tmp_path: Path) -> None:
    """An arm is named after the model it drives, and a model version has a dot in it. A job name, a
    concurrency group, an artifact name and a cache key all take one, so the shape check must not be
    what refuses it."""
    path = _write_configs(
        tmp_path,
        {
            "name": "pi-glm-4.7-flash",
            "is_nightly": False,
            "lane": "openrouter",
            "model": "openrouter/z-ai/glm-4.7-flash",
            "effort": "medium",
        },
    )

    assert [entry.name for entry in ci_matrix.load_harness_configs(path)] == ["pi-glm-4.7-flash"]


def test_load_harness_configs_refuses_a_name_too_long_to_be_an_artifact_name(tmp_path: Path) -> None:
    path = _write_configs(tmp_path, {"name": "a" * 31, "is_nightly": True})

    with pytest.raises(CiMatrixError, match="at most 30"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_the_reserved_oracle_name(tmp_path: Path) -> None:
    """A cell of that name would upload its job directory under the artifact name its own pair's
    oracle pass uses, and both uploads overwrite, so one would silently replace the other."""
    path = _write_configs(tmp_path, {"name": "oracle", "is_nightly": True})

    with pytest.raises(CiMatrixError, match="reserved"):
        ci_matrix.load_harness_configs(path)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("model", "haiku extra"), ("lane", "anthropic;id"), ("effort", "medium\nhigh"), ("key_provider", "open'ai")],
)
def test_load_harness_configs_refuses_a_kwarg_value_the_run_line_cannot_carry(
    tmp_path: Path, field_name: str, value: str
) -> None:
    """A kwarg value becomes one word of `just minds-evals-run`, so anything the shell would read
    itself has to be refused here on the free job rather than minutes into a paid cell."""
    entry = {"name": "broken", "is_nightly": False, "model": "haiku", "effort": "medium", field_name: value}
    path = _write_configs(tmp_path, entry)

    with pytest.raises(CiMatrixError, match="becomes one word of the run line"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_a_key_env_that_is_not_a_variable_name(tmp_path: Path) -> None:
    """It is spelled into the workflow's Vault `secrets:` list and read back with `${!var}`, so a
    newline there names a second secret and anything else is a bad substitution."""
    path = _write_configs(tmp_path, {"name": "broken", "is_nightly": False, "key_env": "mngr/ci/OTHER"})

    with pytest.raises(CiMatrixError, match="names an environment variable"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_a_key_provider_whose_derived_variable_is_not_a_name(tmp_path: Path) -> None:
    """An entry that spells no `key_env` has one derived from its lane and `key_provider`, and on the
    api-key lane that is `<KEY_PROVIDER>_API_KEY`. A provider may carry the slashes and brackets a
    catalog id needs, so the derived name is the one that has to be checked: `a/b` derives
    `A/B_API_KEY`, which names a Vault secret that cannot exist and which `${!var}` refuses outright."""
    path = _write_configs(
        tmp_path, {"name": "pi-slash", "is_nightly": False, "lane": "api-key", "key_provider": "a/b"}
    )

    with pytest.raises(CiMatrixError, match="names an environment variable"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_accepts_the_catalog_ids_real_configs_name(tmp_path: Path) -> None:
    """The bracketed alias and the slashed provider tag are both real catalog ids, so the shape
    check must not be the thing that refuses them."""
    path = _write_configs(
        tmp_path,
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
        {
            "name": "pi-gpt-5-mini",
            "is_nightly": False,
            "lane": "openrouter",
            "model": "openrouter/openai/gpt-5-mini",
            "effort": "medium",
        },
    )

    assert [entry.name for entry in ci_matrix.load_harness_configs(path)] == ["opus-standard", "pi-gpt-5-mini"]


@pytest.mark.parametrize("fast", [False, True])
def test_load_harness_configs_refuses_a_speed_tier_without_a_model(tmp_path: Path, fast: bool) -> None:
    """The workspace's model endpoint applies no axis without a model, and no endpoint reads the live
    choice back to fill one in, so a tier on its own names an arm nothing can drive."""
    path = _write_configs(tmp_path, {"name": "default", "is_nightly": True, "fast": fast})

    with pytest.raises(CiMatrixError, match="needs a model"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_a_duplicate_name(tmp_path: Path) -> None:
    path = _write_configs(
        tmp_path, {"name": "haiku", "is_nightly": True}, {"name": "haiku", "is_nightly": False, "model": "haiku"}
    )

    with pytest.raises(CiMatrixError, match="twice"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_an_entry_the_driver_would_refuse(tmp_path: Path) -> None:
    """The kwarg rules are the driver's, not this module's: a model without an effort is refused by
    the workspace's model endpoint, and refusing it here costs no runner."""
    path = _write_configs(tmp_path, {"name": "haiku", "is_nightly": True, "model": "haiku"})

    with pytest.raises(CiMatrixError, match="needs an effort"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_validates_entries_no_run_would_select(tmp_path: Path) -> None:
    """A config nobody selected tonight is one somebody dispatches tomorrow."""
    path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "broken", "is_nightly": False, "effort": "high"},
    )

    with pytest.raises(CiMatrixError, match="needs a model"):
        ci_matrix.load_harness_configs(path)


def test_load_harness_configs_refuses_an_empty_list(tmp_path: Path) -> None:
    path = _write_configs(tmp_path)

    with pytest.raises(CiMatrixError, match="names no harness configs"):
        ci_matrix.load_harness_configs(path)


def test_load_nightly_suites_reads_each_suites_config_and_arms(tmp_path: Path) -> None:
    config = _write_eval_config(tmp_path, "apps/minds_evals/configs/eval-config-small.json")
    other = _write_eval_config(tmp_path, "apps/minds_evals/configs/eval-config-stepped.json")
    suites_path = _write_suites(
        tmp_path,
        {"config": config, "harness_configs": []},
        {"config": other, "harness_configs": [{"name": "haiku"}, {"name": "opus-standard", "attempts": 3}]},
    )

    suites = ci_matrix.load_nightly_suites(suites_path, tmp_path)

    assert [suite.config for suite in suites] == [config, other]
    assert [(arm.name, int(arm.attempts)) for arm in suites[1].harness_configs] == [("haiku", 1), ("opus-standard", 3)]
    assert suites[0].harness_configs == ()


def test_load_nightly_suites_refuses_a_file_that_is_not_json(tmp_path: Path) -> None:
    suites_path = tmp_path / "nightly_suites.json"
    suites_path.write_text("{not json")

    with pytest.raises(CiMatrixError, match="not valid JSON"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_a_file_that_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(CiMatrixError, match="cannot read"):
        ci_matrix.load_nightly_suites(tmp_path / "absent.json", tmp_path)


def test_load_nightly_suites_refuses_an_unknown_key(tmp_path: Path) -> None:
    """A misspelled key has to be an error rather than a silently ignored one: a suite whose
    `harness_configs` was typed `harness_config` would run every nightly arm against a config that
    was meant for three of them, and the bill is the first thing that would say so."""
    config = _write_eval_config(tmp_path, "configs/eval-config-small.json")
    suites_path = _write_suites(tmp_path, {"config": config, "harness_config": []})

    with pytest.raises(CiMatrixError, match="harness_config"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_an_empty_list(tmp_path: Path) -> None:
    """A run with no suites has no cells, and a run with no cells skips both paid jobs and reports
    every pair as already green -- so it would read as a verified night."""
    suites_path = _write_suites(tmp_path)

    with pytest.raises(CiMatrixError, match="names no suites"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


@pytest.mark.parametrize("bad_config", ["/etc/passwd.json", "configs/eval-config.toml", "-leading-dash.json", ""])
def test_load_nightly_suites_refuses_a_config_that_is_not_a_repo_relative_json_path(
    tmp_path: Path, bad_config: str
) -> None:
    """The path is echoed into every green marker key of the suite and onto the generate command
    line, so it is held to the shape the dispatch input is held to."""
    suites_path = _write_suites(tmp_path, {"config": bad_config})

    with pytest.raises(CiMatrixError, match="repo-relative"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_a_config_that_is_not_in_the_checkout(tmp_path: Path) -> None:
    """Checked here, on the free job, rather than minutes into a paid runner per pair: generation is
    the first thing an oracle pass does and the first thing it would fail at."""
    suites_path = _write_suites(tmp_path, {"config": "configs/eval-config-absent.json"})

    with pytest.raises(CiMatrixError, match="not in the checkout"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_the_same_config_in_two_suites(tmp_path: Path) -> None:
    """Two suites over one config would share an oracle pass, a set of job names and, for any arm
    they both name, a green marker key."""
    config = _write_eval_config(tmp_path, "configs/eval-config-small.json")
    suites_path = _write_suites(
        tmp_path, {"config": config, "harness_configs": [{"name": "haiku"}]}, {"config": config}
    )

    with pytest.raises(CiMatrixError, match="twice"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_two_configs_whose_file_names_shorten_to_one_word(tmp_path: Path) -> None:
    """Only the file name reaches a job name, an artifact name and a summary file name, so two
    configs under different directories with one file name are two suites whose every reported name
    is the same -- including the summary files `notify` merges into one directory, where the second
    upload would silently replace the first."""
    first = _write_eval_config(tmp_path, "configs/eval-config-small.json")
    second = _write_eval_config(tmp_path, "configs/experiments/eval-config-small.json")
    suites_path = _write_suites(tmp_path, {"config": first}, {"config": second})

    with pytest.raises(CiMatrixError, match="shorten to"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_a_config_whose_file_name_is_too_long_to_name_a_job(tmp_path: Path) -> None:
    config = _write_eval_config(tmp_path, "configs/{}.json".format("eval-config-" + "x" * 19))
    suites_path = _write_suites(tmp_path, {"config": config})

    with pytest.raises(CiMatrixError, match="at most 30"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


def test_load_nightly_suites_refuses_the_reserved_oracle_name_as_a_config(tmp_path: Path) -> None:
    """A suite whose slug is `oracle` names its cells' artifacts after its own oracle pass, and both
    uploads overwrite, so one would silently replace the other."""
    config = _write_eval_config(tmp_path, "configs/oracle.json")
    suites_path = _write_suites(tmp_path, {"config": config})

    with pytest.raises(CiMatrixError, match="reserved"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


@pytest.mark.parametrize("bad_attempts", [0, -1, 1.5, "two"])
def test_load_nightly_suites_refuses_an_attempt_count_that_is_not_a_positive_whole_number(
    tmp_path: Path, bad_attempts: object
) -> None:
    """Attempts become harbor's `-k`, which is a sample count: zero of them measures nothing and
    pays for a runner to do it."""
    config = _write_eval_config(tmp_path, "configs/eval-config-small.json")
    suites_path = _write_suites(
        tmp_path, {"config": config, "harness_configs": [{"name": "haiku", "attempts": bad_attempts}]}
    )

    with pytest.raises(CiMatrixError, match="attempts"):
        ci_matrix.load_nightly_suites(suites_path, tmp_path)


@pytest.mark.parametrize(
    ("config_path", "slug"),
    [
        ("apps/minds_evals/configs/eval-config-small.json", "eval-config-small"),
        ("apps/minds_evals/configs/eval-config-time-to-mock.json", "eval-config-time-to-mock"),
        ("configs/Eval_Config.json", "eval-config"),
        ("configs/eval-config-v1.2.json", "eval-config-v1.2"),
    ],
)
def test_the_config_slug_is_the_file_name_as_a_job_name_spells_it(config_path: str, slug: str) -> None:
    """The slug is what every job, artifact and summary name carries in place of the path: one word,
    lowercase, no extension, and a dot kept where a config names a version."""
    assert ci_matrix.config_slug(config_path) == slug


def test_resolve_suites_takes_every_nightly_arm_for_a_suite_that_names_none(tmp_path: Path) -> None:
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "haiku", "is_nightly": True, "model": "haiku", "effort": "medium"},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)

    resolved = ci_matrix.resolve_suites((NightlySuite(config=_CONFIG_PATH),), entries)

    assert [arm.entry.name for arm in resolved[0].arms] == ["default", "haiku"]
    assert resolved[0].config_slug == "nightly"


def test_resolve_suites_takes_a_named_arm_whatever_its_nightly_flag_says(tmp_path: Path) -> None:
    """Which is what a suite's own list is for: the arms worth running against one config, rather
    than the set worth running against every config."""
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)
    suite = NightlySuite(config=_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="opus-standard"),))

    resolved = ci_matrix.resolve_suites((suite,), entries)

    assert [arm.entry.name for arm in resolved[0].arms] == ["opus-standard"]


def test_resolve_suites_keeps_the_order_a_suite_wrote_its_arms_in(tmp_path: Path) -> None:
    """The order decides which cells the matrix lists first, and so the order of the grid's columns
    in the Slack report: a suite that wrote two arms side by side wants to read them that way."""
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "haiku", "is_nightly": True, "model": "haiku", "effort": "medium"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)
    suite = NightlySuite(
        config=_CONFIG_PATH,
        harness_configs=(NightlySuiteArm(name="haiku"), NightlySuiteArm(name="default")),
    )

    resolved = ci_matrix.resolve_suites((suite,), entries)

    assert [arm.entry.name for arm in resolved[0].arms] == ["haiku", "default"]


def test_resolve_suites_refuses_an_arm_the_harness_configs_file_does_not_hold(tmp_path: Path) -> None:
    """The suite file names arms; the harness configs file defines them. A name in one and not the
    other is a cell nothing could drive, and it has to fail before the pairs are frozen."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(configs_path)
    suite = NightlySuite(config=_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="sonnet"),))

    with pytest.raises(CiMatrixError, match="does not hold; it holds default"):
        ci_matrix.resolve_suites((suite,), entries)


def test_resolve_suites_refuses_one_suite_naming_an_arm_twice(tmp_path: Path) -> None:
    """Two cells of one arm share a concurrency group and a green marker key, so the second would
    wait for the first and then claim its verdict."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(configs_path)
    suite = NightlySuite(
        config=_CONFIG_PATH,
        harness_configs=(NightlySuiteArm(name="default"), NightlySuiteArm(name="default", attempts=PositiveInt(2))),
    )

    with pytest.raises(CiMatrixError, match="twice"):
        ci_matrix.resolve_suites((suite,), entries)


def test_resolve_suites_refuses_a_cell_whose_name_would_not_fit_where_it_is_reported(tmp_path: Path) -> None:
    """A cell's name is `<pair>-<config slug>-<arm>`, and both halves are within their own limit
    here: it is the two together, under the longest pair name, that no longer fits a job title."""
    configs_path = _write_configs(tmp_path, {"name": "a" * 30, "is_nightly": True})
    entries = ci_matrix.load_harness_configs(configs_path)
    suite = NightlySuite(
        config="apps/minds_evals/configs/{}.json".format("b" * 30),
        harness_configs=(NightlySuiteArm(name="a" * 30),),
    )

    with pytest.raises(CiMatrixError, match="at most 64"):
        ci_matrix.resolve_suites((suite,), entries)


def test_a_dispatched_config_replaces_the_suite_file_with_one_ad_hoc_suite(tmp_path: Path) -> None:
    """Naming a config is asking about that config, so it runs instead of the checked-in suites --
    on the arms the dispatch named, which is the only way to ask for an arm the suite for that
    config would not have run."""
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)
    checked_in = (NightlySuite(config=_OTHER_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="default"),)),)

    suites = ci_matrix.suites_for_run(
        suites=checked_in, entries=entries, config_path=_CONFIG_PATH, selection="opus-standard"
    )

    assert [suite.config for suite in suites] == [_CONFIG_PATH]
    assert [arm.name for arm in suites[0].harness_configs] == ["opus-standard"]


def test_a_dispatched_config_with_no_arms_runs_the_nightly_set(tmp_path: Path) -> None:
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)

    suites = ci_matrix.suites_for_run(suites=(), entries=entries, config_path=_CONFIG_PATH, selection="")

    assert [arm.name for arm in suites[0].harness_configs] == ["default"]


def test_a_dispatched_arm_list_alone_runs_every_checked_in_config_on_exactly_those_arms(tmp_path: Path) -> None:
    """Which is what makes "try this arm tonight" one input to type: the configs stay the checked-in
    ones, because a dispatch form has no way to say which of them an arm belongs to."""
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "haiku", "is_nightly": True, "model": "haiku", "effort": "medium"},
    )
    entries = ci_matrix.load_harness_configs(configs_path)
    checked_in = (
        NightlySuite(config=_CONFIG_PATH),
        NightlySuite(config=_OTHER_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="default"),)),
    )

    suites = ci_matrix.suites_for_run(suites=checked_in, entries=entries, config_path="", selection="haiku")

    assert [(suite.config, [arm.name for arm in suite.harness_configs]) for suite in suites] == [
        (_CONFIG_PATH, ["haiku"]),
        (_OTHER_CONFIG_PATH, ["haiku"]),
    ]


def test_a_run_that_names_neither_a_config_nor_an_arm_gets_the_suite_file_as_written(tmp_path: Path) -> None:
    """A schedule fires with every input empty, which is the whole point of the file."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(configs_path)
    checked_in = (NightlySuite(config=_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="default"),)),)

    assert ci_matrix.suites_for_run(suites=checked_in, entries=entries, config_path="", selection="") == checked_in


def test_a_dispatched_config_that_is_not_a_repo_relative_json_path_is_refused(tmp_path: Path) -> None:
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(configs_path)

    with pytest.raises(CiMatrixError, match="repo-relative"):
        ci_matrix.suites_for_run(suites=(), entries=entries, config_path="configs/eval-config.toml", selection="")


def test_an_empty_selection_takes_every_nightly_config(tmp_path: Path) -> None:
    path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(path)

    assert [entry.name for entry in ci_matrix.select_harness_configs(entries, "  ")] == ["default"]


def test_a_selection_keeps_the_files_order_whatever_order_it_was_typed_in(tmp_path: Path) -> None:
    """The order decides which cells the matrix lists first, and a dispatch that typed two names
    the other way round is asking for the same run."""
    path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "haiku", "is_nightly": True, "model": "haiku", "effort": "medium"},
    )
    entries = ci_matrix.load_harness_configs(path)

    assert [entry.name for entry in ci_matrix.select_harness_configs(entries, "haiku, default")] == [
        "default",
        "haiku",
    ]


def test_a_selection_naming_one_config_twice_yields_one_cell(tmp_path: Path) -> None:
    path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(path)

    assert [entry.name for entry in ci_matrix.select_harness_configs(entries, "default,,default")] == ["default"]


def test_a_selection_naming_an_unknown_config_is_refused_and_says_what_is_known(tmp_path: Path) -> None:
    path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(path)

    with pytest.raises(CiMatrixError, match="no harness config is named sonnet; the file holds default"):
        ci_matrix.select_harness_configs(entries, "default,sonnet")


@pytest.mark.parametrize("selection", [",", ",,", " , "])
def test_a_selection_of_separators_alone_is_refused_rather_than_run_as_nothing(tmp_path: Path, selection: str) -> None:
    """The dispatch input's own guard accepts separators and spaces alone, and a run with no cells
    skips both paid jobs and reports every pair as already green -- so an empty selection would read
    as a verified night rather than as the typo it is."""
    path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entries = ci_matrix.load_harness_configs(path)

    with pytest.raises(CiMatrixError, match="names no config"):
        ci_matrix.select_harness_configs(entries, selection)


def test_a_selection_can_name_a_config_the_nightly_set_leaves_out(tmp_path: Path) -> None:
    """Which is what a non-nightly entry is for: checked in, costing nothing every night, and
    dispatchable by name."""
    path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    entries = ci_matrix.load_harness_configs(path)

    assert [entry.name for entry in ci_matrix.select_harness_configs(entries, "opus-standard")] == ["opus-standard"]


def test_an_empty_nightly_set_is_refused_rather_than_run_as_nothing(tmp_path: Path) -> None:
    path = _write_configs(
        tmp_path, {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"}
    )
    entries = ci_matrix.load_harness_configs(path)

    with pytest.raises(CiMatrixError, match="nightly"):
        ci_matrix.select_harness_configs(entries, "")


def test_harbor_args_for_a_lane_only_config_names_no_model_axis() -> None:
    """The driver refuses `effort` or `fast` without a model, and a config that names no model has
    to leave the workspace's own tier alone."""
    entry = HarnessConfigEntry(name="default", is_nightly=True, lane="anthropic")

    assert ci_matrix.harbor_args_for(entry) == ("--ak", "lane=anthropic", "--ak", "key_env=ANTHROPIC_API_KEY")


@pytest.mark.parametrize(("fast", "tier"), [(None, "false"), (False, "false"), (True, "true")])
def test_harbor_args_for_a_model_config_spells_the_speed_tier_out(fast: bool | None, tier: str) -> None:
    """A config that names a model but says nothing about the tier runs standard, and the run line
    says so rather than leaving the tier to whatever the template ships. Saying `false` outright
    names the same arm; only `true` moves it, and doubles the arm's rate."""
    entry = HarnessConfigEntry(
        name="haiku", is_nightly=True, lane="anthropic", model="haiku", effort="medium", fast=fast
    )

    assert ci_matrix.harbor_args_for(entry) == (
        "--ak",
        "lane=anthropic",
        "--ak",
        "key_env=ANTHROPIC_API_KEY",
        "--ak",
        "model=haiku",
        "--ak",
        "effort=medium",
        "--ak",
        "fast={}".format(tier),
    )


def test_harbor_args_for_an_api_key_config_carries_its_provider() -> None:
    entry = HarnessConfigEntry(name="pi-openai", is_nightly=False, lane="api-key", key_provider="openai")

    assert ci_matrix.harbor_args_for(entry) == (
        "--ak",
        "lane=api-key",
        "--ak",
        "key_env=OPENAI_API_KEY",
        "--ak",
        "key_provider=openai",
    )


def test_the_cache_key_moves_when_a_config_is_edited_under_the_same_name() -> None:
    """The marker says an arm was verified, and editing a config in place makes a different arm out
    of the same name -- one nothing has ever run."""
    pair = _resolved_pair()
    before = HarnessConfigEntry(name="fast-arm", is_nightly=True, model="haiku", effort="medium")
    after = HarnessConfigEntry(name="fast-arm", is_nightly=True, model="sonnet", effort="medium")

    assert ci_matrix.cache_key_for(pair, _CONFIG_PATH, before) != ci_matrix.cache_key_for(pair, _CONFIG_PATH, after)


def test_the_cache_key_is_exactly_this() -> None:
    """The key is composed once when the run is decided and again when the marker is saved, from
    two processes; anything that moved between them would skip nothing, ever. Pinning the literal
    also pins the format the `gh cache list --key` listing and `matrix.cache_key` both depend on --
    and changing it deliberately abandons every marker already in the cache, which is a decision
    worth making explicitly."""
    pair = _resolved_pair()
    entry = HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium")

    assert ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry) == (
        "minds-evals-green-main-mngr-{}-dwt-{}-cfg-apps-minds-evals-configs-nightly-json-hc-haiku-441f80fea9b0".format(
            _MNGR_SHA, _DWT_SHA
        )
    )


def test_the_cache_key_carries_the_arm_in_readable_form() -> None:
    pair = _resolved_pair()
    entry = HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium")

    key = ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry)

    assert key.startswith("{}main-mngr-{}-dwt-{}-cfg-".format(ci_matrix.GREEN_MARKER_KEY_PREFIX, _MNGR_SHA, _DWT_SHA))
    assert "-cfg-apps-minds-evals-configs-nightly-json-hc-haiku-" in key


def test_read_green_marker_keys_keeps_only_the_refs_this_run_could_restore(tmp_path: Path) -> None:
    """GitHub serves a run only the caches saved on its own ref or on the default branch. A marker
    saved by a feature-branch push is visible in the listing but unrestorable here, and honouring it
    would report an arm green that this run never verified."""
    path = tmp_path / "markers.json"
    path.write_text(
        json.dumps(
            [
                {"key": "minds-evals-green-main-restorable", "ref": "refs/heads/main"},
                {"key": "minds-evals-green-main-own-branch", "ref": "refs/heads/minds-evals-run/try"},
                {"key": "minds-evals-green-main-elsewhere", "ref": "refs/heads/someone-elses-branch"},
            ]
        )
    )

    keys = ci_matrix.read_green_marker_keys(path, ["refs/heads/main", "refs/heads/minds-evals-run/try"])

    assert keys == frozenset({"minds-evals-green-main-restorable", "minds-evals-green-main-own-branch"})


def test_read_green_marker_keys_skips_an_entry_that_names_no_key_or_ref(tmp_path: Path) -> None:
    path = tmp_path / "markers.json"
    path.write_text(json.dumps([{"ref": "refs/heads/main"}, {"key": "minds-evals-green-main-x", "ref": None}]))

    assert ci_matrix.read_green_marker_keys(path, ["refs/heads/main"]) == frozenset()


def test_read_green_marker_keys_refuses_a_file_that_does_not_exist(tmp_path: Path) -> None:
    """A listing that could not be read must not read as "nothing is green": that would re-run
    every arm, which is the expensive mistake."""
    with pytest.raises(CiMatrixError, match="cannot read"):
        ci_matrix.read_green_marker_keys(tmp_path / "absent.json", ["refs/heads/main"])


def test_read_green_marker_keys_refuses_output_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "markers.json"
    path.write_text("gh: not logged in")

    with pytest.raises(CiMatrixError, match="not valid JSON"):
        ci_matrix.read_green_marker_keys(path, ["refs/heads/main"])


def test_read_green_marker_keys_refuses_a_listing_holding_something_other_than_entries(tmp_path: Path) -> None:
    path = tmp_path / "markers.json"
    path.write_text(json.dumps(["minds-evals-green-main-x"]))

    with pytest.raises(CiMatrixError, match="not a cache entry object"):
        ci_matrix.read_green_marker_keys(path, ["refs/heads/main"])


def test_read_green_marker_keys_refuses_output_that_is_not_a_cache_listing(tmp_path: Path) -> None:
    path = tmp_path / "markers.json"
    path.write_text(json.dumps({"key": "minds-evals-green-main-x"}))

    with pytest.raises(CiMatrixError, match="not the JSON array"):
        ci_matrix.read_green_marker_keys(path, ["refs/heads/main"])


def test_read_frozen_pairs_reads_one_pair_per_line(tmp_path: Path) -> None:
    path = _write_pairs(tmp_path, _resolved_pair(), _unresolved_pair())

    pairs = ci_matrix.read_frozen_pairs(path)

    assert [pair.pair for pair in pairs] == ["main", "released"]
    assert [pair.is_resolved for pair in pairs] == [True, False]


def test_read_frozen_pairs_accepts_a_freeze_that_produced_no_pairs(tmp_path: Path) -> None:
    """Which pairs a run asks about is the freeze step's decision, and "none" is one of its answers."""
    path = tmp_path / "pairs.jsonl"
    path.write_text("\n\n")

    assert ci_matrix.read_frozen_pairs(path) == ()


def test_read_frozen_pairs_refuses_a_file_it_cannot_read(tmp_path: Path) -> None:
    """A freeze step that wrote nowhere the reader can reach is not a run that froze no pair: the
    first is a broken run and the second is a decision, and reading them the same way would report
    a whole night as "no pairs were resolved"."""
    with pytest.raises(CiMatrixError, match="cannot read"):
        ci_matrix.read_frozen_pairs(tmp_path)


def test_read_frozen_pairs_refuses_a_line_that_is_not_a_pair(tmp_path: Path) -> None:
    path = tmp_path / "pairs.jsonl"
    path.write_text('{"pair": "main"}\n')

    with pytest.raises(CiMatrixError, match="line 1"):
        ci_matrix.read_frozen_pairs(path)


def test_read_frozen_pairs_refuses_the_same_pair_twice(tmp_path: Path) -> None:
    """Two lines under one name would give that pair two cells per harness config: two evaluate jobs
    sharing one concurrency group and one green marker key, and a summary table listing every cell
    under each of its rows."""
    path = _write_pairs(tmp_path, _resolved_pair(), _resolved_pair())

    with pytest.raises(CiMatrixError, match="line 2"):
        ci_matrix.read_frozen_pairs(path)


def test_a_cell_whose_marker_is_green_is_skipped() -> None:
    entry = HarnessConfigEntry(name="default", is_nightly=True)
    pair = _resolved_pair()
    green = frozenset({ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry)})

    matrix = ci_matrix.decide_matrix(pairs=[pair], suites=[_suite(entry)], green_keys=green, is_forced=False)

    assert [cell.decision for cell in matrix.cells] == [CellDecision.SKIP]
    assert [decided.decision for decided in matrix.pairs] == [PairDecision.SKIP]
    assert matrix.is_any_cell_running is False
    assert matrix.oracle_matrix["include"] == []


def test_force_runs_a_cell_whose_marker_is_green() -> None:
    entry = HarnessConfigEntry(name="default", is_nightly=True)
    pair = _resolved_pair()
    green = frozenset({ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry)})

    matrix = ci_matrix.decide_matrix(pairs=[pair], suites=[_suite(entry)], green_keys=green, is_forced=True)

    assert [cell.decision for cell in matrix.cells] == [CellDecision.RUN]
    assert [decided.decision for decided in matrix.pairs] == [PairDecision.RUN]


def test_a_cell_carries_its_own_suites_config_and_attempts() -> None:
    """Everything a cell's job needs is spelled out in the cell, because the workflow reads values
    and composes nothing: the config it generates from, the one word its job and artifact names
    carry, and how many times harbor runs each case."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)

    matrix = ci_matrix.decide_matrix(
        pairs=[_resolved_pair()],
        suites=[_suite(entry, config="apps/minds_evals/configs/eval-config-time-to-mock.json", attempts=3)],
        green_keys=frozenset(),
        is_forced=False,
    )

    cell = matrix.cells[0]
    assert cell.config == "apps/minds_evals/configs/eval-config-time-to-mock.json"
    assert cell.config_slug == "eval-config-time-to-mock"
    assert int(cell.attempts) == 3


def test_an_arm_that_asks_for_nothing_runs_each_case_once() -> None:
    """One attempt is the count that costs nothing extra, so it is what every arm gets unless a
    suite says otherwise -- and the workflow passes no `-k` at all for it."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)
    suite = NightlySuite(config=_CONFIG_PATH, harness_configs=(NightlySuiteArm(name="default"),))

    resolved = ci_matrix.resolve_suites((suite,), (entry,))
    matrix = ci_matrix.decide_matrix(
        pairs=[_resolved_pair()], suites=resolved, green_keys=frozenset(), is_forced=False
    )

    assert int(matrix.cells[0].attempts) == 1


def test_the_attempt_count_is_not_part_of_the_green_marker_key() -> None:
    """A marker says which arm was verified, and the number of samples a night buys of it is not
    part of which arm it is: keying on it would re-run every cell of a suite the night its attempts
    changed, and re-verify nothing."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)
    pair = _resolved_pair()

    keys = {
        int(attempts): ci_matrix.decide_matrix(
            pairs=[pair], suites=[_suite(entry, attempts=attempts)], green_keys=frozenset(), is_forced=False
        )
        .cells[0]
        .cache_key
        for attempts in (1, 3)
    }

    assert keys[1] == keys[3]


def test_two_suites_give_a_pair_a_cell_of_each_and_an_oracle_pass_of_each() -> None:
    """A suite's oracle exercises its own generated task, verifier container and grading, so one
    suite's pass says nothing about another's -- and a cell gates on the pass of its own config."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)

    matrix = ci_matrix.decide_matrix(
        pairs=[_resolved_pair()],
        suites=[_suite(entry), _suite(entry, config=_OTHER_CONFIG_PATH)],
        green_keys=frozenset(),
        is_forced=False,
    )

    assert [(cell.config_slug, cell.harness_config) for cell in matrix.cells] == [
        ("nightly", "default"),
        ("nightly-stepped", "default"),
    ]
    assert [(entry["pair"], entry["config_slug"]) for entry in matrix.oracle_matrix["include"]] == [
        ("main", "nightly"),
        ("main", "nightly-stepped"),
    ]
    assert matrix.configs == (_CONFIG_PATH, _OTHER_CONFIG_PATH)


def test_a_suite_whose_cells_are_all_green_buys_no_oracle_pass_while_the_other_suite_runs() -> None:
    """The oracle is the expensive pass a night starts with, and it is per suite: a config verified
    last night must not pay for one because another config's arm moved."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)
    pair = _resolved_pair()
    green = frozenset({ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry)})

    matrix = ci_matrix.decide_matrix(
        pairs=[pair],
        suites=[_suite(entry), _suite(entry, config=_OTHER_CONFIG_PATH)],
        green_keys=green,
        is_forced=False,
    )

    assert [(cell.config_slug, cell.decision) for cell in matrix.cells] == [
        ("nightly", CellDecision.SKIP),
        ("nightly-stepped", CellDecision.RUN),
    ]
    assert [entry["config_slug"] for entry in matrix.oracle_matrix["include"]] == ["nightly-stepped"]


def test_a_pair_runs_when_any_one_of_its_cells_does() -> None:
    """A suite's oracle pass serves every cell of that suite, so one unverified arm is enough to
    make the pair run."""
    entries = (
        HarnessConfigEntry(name="default", is_nightly=True),
        HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium"),
    )
    pair = _resolved_pair()
    green = frozenset({ci_matrix.cache_key_for(pair, _CONFIG_PATH, entries[0])})

    matrix = ci_matrix.decide_matrix(pairs=[pair], suites=[_suite(*entries)], green_keys=green, is_forced=False)

    assert [(cell.harness_config, cell.decision) for cell in matrix.cells] == [
        ("default", CellDecision.SKIP),
        ("haiku", CellDecision.RUN),
    ]
    assert [decided.decision for decided in matrix.pairs] == [PairDecision.RUN]


def test_an_unresolved_pair_carries_no_cells_and_leaves_the_others_alone() -> None:
    """A pair with no SHAs has nothing to check out and nothing to key a marker on, so it is
    reported rather than aborting the run: the pairs answer different questions."""
    entry = HarnessConfigEntry(name="default", is_nightly=True)

    matrix = ci_matrix.decide_matrix(
        pairs=[_unresolved_pair(), _resolved_pair()],
        suites=[_suite(entry)],
        green_keys=frozenset(),
        is_forced=False,
    )

    assert [(decided.pair, decided.decision) for decided in matrix.pairs] == [
        ("released", PairDecision.UNRESOLVED),
        ("main", PairDecision.RUN),
    ]
    assert [cell.pair for cell in matrix.cells] == ["main"]


def test_the_job_matrices_hold_only_what_runs() -> None:
    """The two matrices are what the `oracle` and `evaluate` jobs fan out over, so a skipped cell
    reaching either of them would pay for an arm the run decided not to evaluate."""
    entries = (
        HarnessConfigEntry(name="default", is_nightly=True),
        HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium"),
    )
    running_pair = _resolved_pair()
    green_pair = _resolved_pair(pair="released", mngr_sha="c" * 40, dwt_sha="d" * 40)
    green = frozenset(ci_matrix.cache_key_for(green_pair, _CONFIG_PATH, entry) for entry in entries)

    matrix = ci_matrix.decide_matrix(
        pairs=[running_pair, green_pair, _unresolved_pair(pair="custom")],
        suites=[_suite(*entries)],
        green_keys=green,
        is_forced=False,
    )

    assert [entry["pair"] for entry in matrix.oracle_matrix["include"]] == ["main"]
    assert [(entry["pair"], entry["harness_config"]) for entry in matrix.matrix["include"]] == [
        ("main", "default"),
        ("main", "haiku"),
    ]
    assert all("decision" not in entry for entry in matrix.matrix["include"])


def test_a_cells_harbor_args_decode_back_to_the_argv_the_run_line_takes() -> None:
    """A GitHub Actions matrix entry can only carry strings, so the arguments ride as JSON and the
    cell's job decodes them; what comes back has to be exactly the argv."""
    entry = HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium")

    matrix = ci_matrix.decide_matrix(
        pairs=[_resolved_pair()],
        suites=[_suite(entry)],
        green_keys=frozenset(),
        is_forced=False,
    )

    cell = matrix.cells[0]
    # Spelled out rather than compared against `harbor_args_for`, which is what built the field:
    # comparing the two would test only that JSON round-trips.
    assert json.loads(cell.harbor_args) == [
        "--ak",
        "lane=anthropic",
        "--ak",
        "key_env=ANTHROPIC_API_KEY",
        "--ak",
        "model=haiku",
        "--ak",
        "effort=medium",
        "--ak",
        "fast=false",
    ]
    assert cell.lane_key_env == "ANTHROPIC_API_KEY"
    assert cell.config == _CONFIG_PATH


def test_the_summary_names_every_arm_and_every_unresolved_pair() -> None:
    """The table is what a human opens to see where a night's money went, so it carries every
    decision, `skip` included -- that is the row an ordinary night is mostly made of, and the one
    the two bullets under the table exist to explain."""
    entries = (
        HarnessConfigEntry(name="default", is_nightly=True),
        HarnessConfigEntry(name="haiku", is_nightly=True, model="haiku", effort="medium"),
    )
    pair = _resolved_pair()

    matrix = ci_matrix.decide_matrix(
        pairs=[pair, _unresolved_pair()],
        suites=[_suite(*entries)],
        green_keys=frozenset({ci_matrix.cache_key_for(pair, _CONFIG_PATH, entries[1])}),
        is_forced=False,
    )
    summary = ci_matrix.render_matrix_summary_markdown(matrix, "imbue-ai/mngr-internal")

    assert summary.startswith("## Arms")
    assert (
        "| `main` | `nightly` | `default` | `main` (`aaaaaaaaaaaa`) | `main` (`bbbbbbbbbbbb`) | **run** |" in summary
    )
    assert "| `main` | `nightly` | `haiku` | `main` (`aaaaaaaaaaaa`) | `main` (`bbbbbbbbbbbb`) | **skip** |" in summary
    assert (
        "| `released` | - | - | `minds-v9.9.9` (`unresolved`) | `minds-v9.9.9` (`unresolved`) | **unresolved** |"
        in summary
    )
    assert "- configs: `{}`".format(_CONFIG_PATH) in summary
    assert "gh cache list --repo imbue-ai/mngr-internal --key minds-evals-green-" in summary


def _invoke_ci_matrix(pairs_path: Path, configs_path: Path, output_path: Path, *extra_arguments: str) -> Result:
    """The command with the arguments the workflow's resolve step always passes, plus whatever the
    caller is varying. The config makes it one ad-hoc suite over the harness configs file the test
    wrote, which is what a dispatch naming a config gets."""
    return CliRunner().invoke(
        main,
        [
            "ci-matrix",
            "--pairs",
            str(pairs_path),
            "--harness-configs",
            str(configs_path),
            "--config",
            _CONFIG_PATH,
            "--output",
            str(output_path),
            *extra_arguments,
        ],
    )


def test_ci_matrix_decides_the_checked_in_suites_when_a_dispatch_names_no_config(tmp_path: Path) -> None:
    """End to end over the files a schedule actually reads: both checked-in files and the frozen
    pairs, with no dispatch input at all. Every cell of every suite, at the arms the suites name."""
    pairs_path = _write_pairs(tmp_path, _resolved_pair())
    output_path = tmp_path / "matrix.json"

    result = CliRunner().invoke(
        main,
        ["ci-matrix", "--pairs", str(pairs_path), "--config", "", "--output", str(output_path)],
    )

    assert result.exit_code == 0, result.output
    decided = json.loads(output_path.read_text())
    assert [(cell["config_slug"], cell["harness_config"]) for cell in decided["cells"]] == [
        ("eval-config-small", "default"),
        ("eval-config-small", "haiku"),
        ("eval-config-small", "pi-gpt-5-mini"),
        ("eval-config-small", "codex-sol-low"),
        ("eval-config-small", "codex-terra"),
        ("eval-config-time-to-mock", "opus-standard"),
        ("eval-config-time-to-mock", "codex-sol-low"),
        ("eval-config-time-to-mock", "codex-terra"),
    ]
    assert [entry["config_slug"] for entry in decided["oracle_matrix"]["include"]] == [
        "eval-config-small",
        "eval-config-time-to-mock",
    ]


def test_ci_matrix_refuses_a_suite_file_naming_a_config_that_is_not_in_the_checkout(tmp_path: Path) -> None:
    """The person who reaches this edited the suite file, and the run they broke is the whole
    nightly: it reads as a usage error naming the config rather than as a traceback."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    suites_path = _write_suites(tmp_path, {"config": "apps/minds_evals/configs/eval-config-absent.json"})
    pairs_path = _write_pairs(tmp_path, _resolved_pair())

    result = CliRunner().invoke(
        main,
        [
            "ci-matrix",
            "--pairs",
            str(pairs_path),
            "--harness-configs",
            str(configs_path),
            "--nightly-suites",
            str(suites_path),
            "--config",
            "",
            "--output",
            str(tmp_path / "matrix.json"),
        ],
    )

    assert result.exit_code != 0
    assert "eval-config-absent.json" in result.output


def test_ci_matrix_writes_the_decision_and_the_summary(tmp_path: Path) -> None:
    configs_path = _write_configs(
        tmp_path,
        {"name": "default", "is_nightly": True},
        {"name": "haiku", "is_nightly": True, "model": "haiku", "effort": "medium"},
        {"name": "opus-standard", "is_nightly": False, "model": "opus[1m]", "effort": "high"},
    )
    pairs_path = _write_pairs(tmp_path, _resolved_pair(), _unresolved_pair())
    output_path = tmp_path / "out" / "matrix.json"
    summary_path = tmp_path / "out" / "summary.md"

    result = _invoke_ci_matrix(
        pairs_path,
        configs_path,
        output_path,
        "--summary-md",
        str(summary_path),
        "--repository",
        "imbue-ai/mngr-internal",
    )

    assert result.exit_code == 0, result.output
    decided = json.loads(output_path.read_text())
    assert [(cell["pair"], cell["config_slug"], cell["harness_config"]) for cell in decided["cells"]] == [
        ("main", "nightly", "default"),
        ("main", "nightly", "haiku"),
    ]
    assert [(pair["pair"], pair["decision"]) for pair in decided["pairs"]] == [
        ("main", "run"),
        ("released", "unresolved"),
    ]
    assert decided["is_any_cell_running"] is True
    assert summary_path.read_text().startswith("## Arms")


def test_ci_matrix_skips_a_cell_whose_marker_this_run_can_restore(tmp_path: Path) -> None:
    """End to end over the two files the workflow hands it: the frozen pairs and the cache listing."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entry = ci_matrix.load_harness_configs(configs_path)[0]
    pair = _resolved_pair()
    pairs_path = _write_pairs(tmp_path, pair)
    markers_path = tmp_path / "markers.json"
    markers_path.write_text(
        json.dumps([{"key": ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry), "ref": "refs/heads/main"}])
    )
    output_path = tmp_path / "matrix.json"

    result = _invoke_ci_matrix(
        pairs_path,
        configs_path,
        output_path,
        "--green-markers",
        str(markers_path),
        "--restorable-ref",
        "refs/heads/main",
    )

    assert result.exit_code == 0, result.output
    decided = json.loads(output_path.read_text())
    assert [cell["decision"] for cell in decided["cells"]] == ["skip"]
    assert decided["is_any_cell_running"] is False
    assert decided["matrix"]["include"] == []


def test_ci_matrix_force_runs_a_green_cell_from_the_command_line(tmp_path: Path) -> None:
    """The workflow composes `--force` or `--no-force` in shell from the dispatch input, so both
    spellings have to stay spellings this command accepts."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    entry = ci_matrix.load_harness_configs(configs_path)[0]
    pair = _resolved_pair()
    pairs_path = _write_pairs(tmp_path, pair)
    markers_path = tmp_path / "markers.json"
    markers_path.write_text(
        json.dumps([{"key": ci_matrix.cache_key_for(pair, _CONFIG_PATH, entry), "ref": "refs/heads/main"}])
    )

    decisions = []
    for force_flag in ("--no-force", "--force"):
        output_path = tmp_path / "matrix{}.json".format(force_flag)
        result = _invoke_ci_matrix(
            pairs_path,
            configs_path,
            output_path,
            "--green-markers",
            str(markers_path),
            "--restorable-ref",
            "refs/heads/main",
            force_flag,
        )
        assert result.exit_code == 0, result.output
        decisions.append(json.loads(output_path.read_text())["cells"][0]["decision"])

    assert decisions == ["skip", "run"]


def test_ci_matrix_refuses_an_unknown_selection_as_a_usage_error(tmp_path: Path) -> None:
    """The person who reaches this mistyped a `--select` name on a dispatch form, so it reads as a
    usage error naming what they typed rather than as a traceback."""
    configs_path = _write_configs(tmp_path, {"name": "default", "is_nightly": True})
    pairs_path = _write_pairs(tmp_path, _resolved_pair())

    result = _invoke_ci_matrix(pairs_path, configs_path, tmp_path / "matrix.json", "--select", "sonnet")

    assert result.exit_code != 0
    assert "sonnet" in result.output


def test_the_workflow_reads_the_decision_by_names_this_package_still_writes() -> None:
    """The resolve job hands its decision to the jobs after it through `jq`, by string key, because
    a workflow cannot import Python. Renaming `is_any_cell_running` is silent AND fail-open: jq
    prints `null`, the `any == 'true'` guard is false, both paid jobs skip, and the nightly stays
    green having measured nothing. So the names are pinned from this side."""
    names_read = set(re.findall(r'jq -[a-z]+ \.([a-z_]+) "\$DECISION_PATH"', read_scheduled_workflow_text()))

    # A rewrite that renamed the step's own variables would otherwise leave this passing over
    # nothing at all.
    assert names_read, "no decision reads found in {}".format(SCHEDULED_WORKFLOW_PATH)
    known = set(CiMatrix.model_fields) | set(CiMatrix.model_computed_fields)
    assert names_read <= known, "CiMatrix does not carry {}".format(sorted(names_read - known))


def _job_block(workflow_text: str, job_name: str) -> str:
    """One job's YAML, from its key to the start of the next one. A job key is the only thing in the
    file indented by exactly two spaces, which is what makes the split possible without a parser."""
    match = re.search(r"^  {}:\n(?:.*\n)*?(?=^  [a-z_]+:\n|\Z)".format(job_name), workflow_text, re.MULTILINE)
    assert match is not None, "no {} job in {}".format(job_name, SCHEDULED_WORKFLOW_PATH)
    return match.group(0)


def _matrix_fields_read(text: str) -> set[str]:
    # The lookbehind keeps `${{ runner.temp }}/matrix.json` out of the field set.
    return set(re.findall(r"(?<![/\w])matrix\.([a-z_]+)", text))


def test_the_workflow_reads_matrix_entries_by_names_the_entry_it_fans_out_over_carries() -> None:
    """Every job below `resolve` composes its checkout, its secrets, its run line and its cache key
    out of `matrix.<field>`. A renamed field interpolates empty rather than failing, which puts an
    empty Vault path or an empty marker key on a paid runner.

    The two matrix jobs fan out over different entries -- `oracle` over a pair, `evaluate` over a
    cell -- so each is held to the one it is actually given. A cell's field read in the oracle job
    is empty there, and checking both jobs against the wider of the two models would not see it.
    """
    workflow_text = read_scheduled_workflow_text()
    oracle_block = _job_block(workflow_text, "oracle")
    evaluate_block = _job_block(workflow_text, "evaluate")
    oracle_fields = _matrix_fields_read(oracle_block)
    evaluate_fields = _matrix_fields_read(evaluate_block)

    # A rewrite that stopped reading the matrix this way would otherwise leave this passing over
    # nothing at all.
    assert oracle_fields, "no matrix fields found in the oracle job"
    assert evaluate_fields, "no matrix fields found in the evaluate job"
    # `oracle_matrix` dumps one OraclePass per (pair, eval config) with a cell to run.
    assert oracle_fields <= set(OraclePass.model_fields), "an oracle entry does not carry {}".format(
        sorted(oracle_fields - set(OraclePass.model_fields))
    )
    cell_fields = set(MatrixCell.model_fields) - {"decision"}
    assert evaluate_fields <= cell_fields, "a cell entry does not carry {}".format(
        sorted(evaluate_fields - cell_fields)
    )
    # Only those two jobs fan out over a matrix, so a `matrix.` read anywhere else names nothing.
    outside = workflow_text.replace(oracle_block, "").replace(evaluate_block, "")
    assert not _matrix_fields_read(outside), "a job that is not a matrix job reads {}".format(
        sorted(_matrix_fields_read(outside))
    )


def test_the_workflow_validates_the_harness_configs_file_this_package_defaults_to() -> None:
    """The nightly reads the file named here, and every test above validates the one
    `CHECKED_IN_HARNESS_CONFIGS_PATH` names. Two different files would leave the tests green over a
    file no run reads."""
    repo_root = SCHEDULED_WORKFLOW_PATH.parents[2]
    relative_path = ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH.relative_to(repo_root)

    assert "HARNESS_CONFIGS: {}\n".format(relative_path) in read_scheduled_workflow_text()


def test_the_workflow_validates_the_suite_file_this_package_defaults_to() -> None:
    """The nightly reads the file named here, and the tests above validate the one
    `CHECKED_IN_NIGHTLY_SUITES_PATH` names. Two different files would leave the tests green over a
    file no run reads."""
    repo_root = SCHEDULED_WORKFLOW_PATH.parents[2]
    relative_path = ci_matrix.CHECKED_IN_NIGHTLY_SUITES_PATH.relative_to(repo_root)

    assert "NIGHTLY_SUITES: {}\n".format(relative_path) in read_scheduled_workflow_text()


def test_the_workflow_asks_harbor_for_more_attempts_only_when_a_suite_wants_them() -> None:
    """`-k` is a sample count, and every extra sample is another box per case: passing it at one
    would cost nothing but say nothing either, and passing an empty value would fail the run. The
    flag is composed in shell from the cell's own attempts, so what the count means is pinned from
    this side."""
    workflow_text = read_scheduled_workflow_text()

    assert "ATTEMPTS: ${{ matrix.attempts }}" in workflow_text
    assert "if (( ATTEMPTS > 1 )); then" in workflow_text
    assert 'attempt_args=(-k "$ATTEMPTS")' in workflow_text
    # Concurrency covers every attempt of every case, because the job's timeout is written for one
    # wave: leaving it at the case count would run the attempts in series and time the cell out.
    assert '"$(( CASE_COUNT * ATTEMPTS ))"' in workflow_text


def test_the_workflow_gates_every_cell_on_the_oracle_pass_of_its_own_eval_config() -> None:
    """An oracle pass exercises its config's generated task, verifier container and grading, so
    another config's pass says nothing about this cell's. The gate is an artifact name and a file
    name composed in YAML, and reading the wrong one is silent: a cell would start on a suite whose
    infrastructure was never checked, or refuse to start on one that passed."""
    workflow_text = read_scheduled_workflow_text()
    evaluate_block = _job_block(workflow_text, "evaluate")

    assert "name: minds-evals-summary-${{ matrix.pair }}-${{ matrix.config_slug }}-oracle" in evaluate_block
    assert (
        "ORACLE_SUMMARY: ${{ runner.temp }}/oracle-summary/"
        "oracle-summary-${{ matrix.pair }}-${{ matrix.config_slug }}.json" in evaluate_block
    )


def test_the_workflow_lists_the_markers_under_the_prefix_this_package_writes() -> None:
    """The listing and the save are two spellings of one prefix, and only the save side is composed
    from the constant. Change the constant alone and `gh cache list` matches nothing while the cells
    go on saving under the new prefix: every arm re-runs at full cost, every night, forever, with
    nothing anywhere to say so."""
    assert "--key {} ".format(ci_matrix.GREEN_MARKER_KEY_PREFIX) in read_scheduled_workflow_text()
