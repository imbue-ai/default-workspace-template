import ast
import json
import sys
from pathlib import Path

import pytest

from env_converge import template_manifest
from env_converge.template_manifest import CURRENT_MANIFEST_FORMAT
from env_converge.template_manifest import MANIFEST_MARKDOWN_NAME
from env_converge.template_manifest import MANIFEST_THUMBNAIL_NAME
from env_converge.template_manifest import MANIFEST_TOML_NAME
from env_converge.template_manifest import Requirements
from env_converge.template_manifest import TemplateManifest
from env_converge.template_manifest import TemplateManifestNotFoundError
from env_converge.template_manifest import TemplateManifestParseError
from env_converge.template_manifest import check_env_d_units
from env_converge.template_manifest import check_markdown_agreement
from env_converge.template_manifest import check_unfinished_placeholders
from env_converge.template_manifest import find_manifest_path
from env_converge.template_manifest import load_template_manifest
from env_converge.template_manifest import validate_template_tree

_MINIMAL_TOML = """
format = "v2"

[template]
slug = "slack-inbox"
title = "Slack Inbox"
description = "A daily digest."
thumbnail = "template.svg"
version = "v1"

[recipe]
include = ["system/apps/slack_inbox"]
"""

_MINIMAL_MARKDOWN = """---
title: Slack Inbox
description: A daily digest.
thumbnail: template.svg
format: v2
---

# Slack Inbox
"""


def _write_tree(
    root: Path,
    toml_text: str = _MINIMAL_TOML,
    markdown_text: str = _MINIMAL_MARKDOWN,
    thumbnail_text: str = "<svg></svg>",
) -> Path:
    (root / MANIFEST_TOML_NAME).write_text(toml_text)
    (root / MANIFEST_MARKDOWN_NAME).write_text(markdown_text)
    (root / MANIFEST_THUMBNAIL_NAME).write_text(thumbnail_text)
    return root


def _manifest(toml_text: str, tmp_path: Path) -> TemplateManifest:
    path = tmp_path / MANIFEST_TOML_NAME
    path.write_text(toml_text)
    return load_template_manifest(path)


# --- the schema itself ---


def test_a_minimal_manifest_loads_with_the_documented_defaults(tmp_path: Path) -> None:
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    assert manifest.format == CURRENT_MANIFEST_FORMAT
    assert manifest.template.slug == "slack-inbox"
    assert manifest.recipe.include == ("system/apps/slack_inbox",)
    # Everything optional defaults to empty rather than requiring boilerplate:
    # most templates declare no environment at all, and that must stay the
    # cheap case.
    assert manifest.recipe.exclude == ()
    assert manifest.requirements.permission == ()
    assert manifest.requirements.llm is None
    assert manifest.lineage == ()
    assert manifest.environment.is_empty()


def test_the_full_environment_and_lineage_shape_round_trips(tmp_path: Path) -> None:
    manifest = _manifest(
        _MINIMAL_TOML
        + """
[requirements.llm]
method = "keyed"

[[requirements.permission]]
scope = "slack-api"
permission = "slack-read-all"

[[requirements.secret]]
name = "SLACK_SIGNING_SECRET"

[environment]
apt_snapshot_timestamp = "20260725T000000Z"
apt = ["poppler-utils"]
cargo_default_toolchain = "stable-x86_64-unknown-linux-gnu"
env_d_units = ["system/scripts/env.d/2000-slack-inbox-fonts.sh"]

[environment.npm_global]
"@slack/cli" = "2.1.0"

[environment.uv_tools]
yt-dlp = "2026.7.1"

[environment.cargo_crates]
fd-find = "9.0.0"

[[lineage]]
slug = "note-taker"
repo_url = "https://github.com/someone/note-taker"
commit = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
used_on = "2026-08-04"
""",
        tmp_path,
    )

    assert manifest.environment.apt == ("poppler-utils",)
    assert manifest.environment.npm_global == {"@slack/cli": "2.1.0"}
    assert manifest.environment.uv_tools == {"yt-dlp": "2026.7.1"}
    assert manifest.environment.cargo_crates == {"fd-find": "9.0.0"}
    assert not manifest.environment.is_empty()
    assert manifest.requirements.llm is not None
    assert manifest.requirements.llm.method == "keyed"
    assert manifest.lineage[0].commit == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    assert manifest.lineage[0].used_on is not None
    assert manifest.lineage[0].used_on.year == 2026


def test_the_publishers_timestamp_is_provenance_and_never_constrains_the_adopter(
    tmp_path: Path,
) -> None:
    # apt is declared as bare names precisely so the versions come from whoever
    # converges them. If a version ever crept into this field the portability
    # story would silently break, so the type must keep rejecting it.
    manifest = _manifest(
        _MINIMAL_TOML + '\n[environment]\napt = ["ripgrep"]\n', tmp_path
    )
    assert manifest.environment.apt == ("ripgrep",)
    assert manifest.environment.apt_snapshot_timestamp is None


@pytest.mark.parametrize(
    "bad_fragment",
    [
        pytest.param('slug = "-leading-dash"', id="slug_leading_dash"),
        pytest.param('slug = "has spaces"', id="slug_spaces"),
        pytest.param('slug = "has/slash"', id="slug_slash"),
        pytest.param('version = "1"', id="version_missing_v"),
        pytest.param('version = "v0"', id="version_zero"),
        pytest.param('title = ""', id="empty_title"),
    ],
)
def test_malformed_identity_fields_are_rejected(
    bad_fragment: str, tmp_path: Path
) -> None:
    key = bad_fragment.split(" =")[0]
    toml_text = "\n".join(
        bad_fragment if line.startswith(f"{key} =") else line
        for line in _MINIMAL_TOML.splitlines()
    )

    with pytest.raises(TemplateManifestParseError):
        _manifest(toml_text, tmp_path)


def test_an_unknown_key_is_rejected_rather_than_silently_ignored(
    tmp_path: Path,
) -> None:
    # extra="forbid" is what turns a typo'd declaration into a publish-time
    # failure instead of a dependency that silently never installs.
    with pytest.raises(TemplateManifestParseError):
        _manifest(
            _MINIMAL_TOML + '\n[environment]\napt_packages = ["ripgrep"]\n', tmp_path
        )


def test_a_bad_snapshot_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TemplateManifestParseError):
        _manifest(
            _MINIMAL_TOML + '\n[environment]\napt_snapshot_timestamp = "2026-07-25"\n',
            tmp_path,
        )


def test_malformed_toml_reports_the_path_and_the_reason(tmp_path: Path) -> None:
    with pytest.raises(TemplateManifestParseError) as excinfo:
        _manifest("[template\nslug =", tmp_path)

    assert MANIFEST_TOML_NAME in str(excinfo.value)
    assert "not valid TOML" in str(excinfo.value)


def test_a_missing_manifest_raises_the_not_found_error(tmp_path: Path) -> None:
    with pytest.raises(TemplateManifestNotFoundError):
        load_template_manifest(tmp_path / MANIFEST_TOML_NAME)


def test_find_manifest_path_treats_absence_as_normal(tmp_path: Path) -> None:
    # An ordinary workspace has no template, and a v1 template repo has
    # slug-named markdown and no TOML -- neither is an error condition.
    assert find_manifest_path(tmp_path) is None

    (tmp_path / MANIFEST_TOML_NAME).write_text(_MINIMAL_TOML)
    assert find_manifest_path(tmp_path) == tmp_path / MANIFEST_TOML_NAME


def test_a_v1_repo_with_slug_named_manifests_is_not_mistaken_for_v2(
    tmp_path: Path,
) -> None:
    # The v1 names, as they actually are in repos published before the rename.
    (tmp_path / "inspiration-slack-inbox.md").write_text(_MINIMAL_MARKDOWN)
    (tmp_path / "inspiration-slack-inbox.svg").write_text("<svg></svg>")

    assert find_manifest_path(tmp_path) is None


# --- env.d unit checks ---


def _manifest_with_units(
    units: list[str], include: list[str], tmp_path: Path
) -> TemplateManifest:
    include_toml = ", ".join(f'"{path}"' for path in include)
    units_toml = ", ".join(f'"{unit}"' for unit in units)
    return _manifest(
        _MINIMAL_TOML.replace(
            'include = ["system/apps/slack_inbox"]', f"include = [{include_toml}]"
        )
        + f"\n[environment]\nenv_d_units = [{units_toml}]\n",
        tmp_path,
    )


def test_a_well_formed_env_d_unit_passes(tmp_path: Path) -> None:
    manifest = _manifest_with_units(
        ["system/scripts/env.d/2000-slack-inbox-fonts.sh"],
        ["system/scripts/env.d/2000-slack-inbox-fonts.sh"],
        tmp_path,
    )

    assert check_env_d_units(manifest) == ()


def test_a_unit_outside_the_env_d_directory_is_flagged(tmp_path: Path) -> None:
    manifest = _manifest_with_units(
        ["system/scripts/setup-fonts.sh"], ["system/scripts"], tmp_path
    )

    problems = check_env_d_units(manifest)
    assert len(problems) == 1
    assert "must live under" in problems[0]


def test_a_unit_ordered_before_the_reserved_range_is_flagged(tmp_path: Path) -> None:
    # Below 2000 a template's unit would interleave with the template's own
    # units, which is exactly the shared-ordering collision the convention exists
    # to prevent.
    manifest = _manifest_with_units(
        ["system/scripts/env.d/1050-slack-inbox-fonts.sh"],
        ["system/scripts/env.d/1050-slack-inbox-fonts.sh"],
        tmp_path,
    )

    problems = check_env_d_units(manifest)
    assert any("2000" in problem for problem in problems)


def test_a_declared_unit_the_recipe_does_not_ship_is_flagged(tmp_path: Path) -> None:
    # The manifest would promise the adopter a setup step that never arrives.
    manifest = _manifest_with_units(
        ["system/scripts/env.d/2000-slack-inbox-fonts.sh"],
        ["system/apps/slack_inbox"],
        tmp_path,
    )

    problems = check_env_d_units(manifest)
    assert any("not covered by the recipe" in problem for problem in problems)


def test_a_unit_covered_by_a_parent_include_path_is_accepted(tmp_path: Path) -> None:
    manifest = _manifest_with_units(
        ["system/scripts/env.d/2000-slack-inbox-fonts.sh"],
        ["system/scripts/env.d"],
        tmp_path,
    )

    assert check_env_d_units(manifest) == ()


# --- markdown / toml agreement ---


def test_matching_markdown_and_toml_agree(tmp_path: Path) -> None:
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    assert check_markdown_agreement(manifest, _MINIMAL_MARKDOWN) == ()


def test_a_hand_edited_markdown_title_is_caught(tmp_path: Path) -> None:
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    problems = check_markdown_agreement(
        manifest,
        _MINIMAL_MARKDOWN.replace("title: Slack Inbox", "title: Something Else"),
    )

    assert any("title" in problem for problem in problems)


def test_a_prerequisite_present_in_only_one_file_is_caught(tmp_path: Path) -> None:
    # This is the gap that actually breaks adoption: the adopting agent acts on
    # the markdown's requires_ lines, so a permission declared in only one place
    # means either a silently-missed setup step or a lie about what is needed.
    manifest = _manifest(
        _MINIMAL_TOML
        + '\n[[requirements.permission]]\nscope = "slack-api"\npermission = "slack-read-all"\n',
        tmp_path,
    )

    problems = check_markdown_agreement(manifest, _MINIMAL_MARKDOWN)

    assert any("requires_permission" in problem for problem in problems)


def test_an_llm_dependency_in_only_one_file_is_caught(tmp_path: Path) -> None:
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    problems = check_markdown_agreement(
        manifest, _MINIMAL_MARKDOWN + "\n- requires_llm: calls Claude via litellm\n"
    )

    assert any("LLM access" in problem for problem in problems)


def test_example_requires_lines_inside_fill_in_comments_are_not_counted(
    tmp_path: Path,
) -> None:
    # The generated FILL-IN instructions quote example requires_ lines to show
    # the form. Counting those made a freshly-generated skeleton -- which
    # declares nothing yet -- look like it declared three of them, so
    # build_template.sh failed its own validation gate on every publish.
    # Caught by running the assembly script end to end, not by the unit tests.
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    markdown = (
        _MINIMAL_MARKDOWN
        + """
## Prerequisites

<!-- FILL-IN (publishing agent): replace this with one line per requirement:

- requires_permission: <latchkey scope> / <permission schema>
- requires_secret: <ENV_VAR or config key>
- requires_llm: <how the code reaches Claude>
-->
"""
    )

    assert check_markdown_agreement(manifest, markdown) == ()


def test_declared_activation_requirements_matching_the_markdown_pass(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        _MINIMAL_TOML
        + '\n[requirements.llm]\nmethod = "keyed"\n'
        + '\n[[requirements.permission]]\nscope = "slack-api"\npermission = "slack-read-all"\n'
        + '\n[[requirements.secret]]\nname = "SLACK_SIGNING_SECRET"\n',
        tmp_path,
    )

    markdown = (
        _MINIMAL_MARKDOWN
        + "\n- requires_permission: slack-api / slack-read-all\n"
        + "- requires_secret: SLACK_SIGNING_SECRET\n"
        + "- requires_llm: calls Claude via the keyed litellm path\n"
    )

    assert check_markdown_agreement(manifest, markdown) == ()


# --- placeholders ---


def test_unreplaced_placeholders_are_caught() -> None:
    assert check_unfinished_placeholders("all done") == ()
    assert check_unfinished_placeholders("<!-- FILL-IN (publishing agent): do it -->")
    assert check_unfinished_placeholders("<!-- minds-placeholder-thumbnail -->")


# --- the whole tree ---


def test_a_complete_tree_validates_clean(tmp_path: Path) -> None:
    _write_tree(tmp_path)

    assert validate_template_tree(tmp_path) == ()


_UNIT_PATH = "system/scripts/env.d/2000-slack-inbox-fonts.sh"
_TOML_WITH_UNIT = (
    _MINIMAL_TOML.replace(
        'include = ["system/apps/slack_inbox"]',
        'include = ["system/apps/slack_inbox", "system/scripts/env.d"]',
    )
    + f'\n[environment]\nenv_d_units = ["{_UNIT_PATH}"]\n'
)


def test_a_declared_env_d_unit_that_is_not_in_the_tree_is_flagged(
    tmp_path: Path,
) -> None:
    """Being named correctly is not the same as being there.

    The name checks prove a unit WOULD ship if it existed. A typo in the path
    passes every one of them and then simply never runs on the adopter's
    machine -- the exact class of surprise the manifest exists to remove.
    """
    _write_tree(tmp_path, toml_text=_TOML_WITH_UNIT)

    problems = validate_template_tree(tmp_path)

    assert any(
        _UNIT_PATH in problem and "not in the tree" in problem for problem in problems
    )


def test_a_declared_env_d_unit_that_is_in_the_tree_passes(tmp_path: Path) -> None:
    _write_tree(tmp_path, toml_text=_TOML_WITH_UNIT)
    unit = tmp_path / _UNIT_PATH
    unit.parent.mkdir(parents=True)
    unit.write_text("#!/usr/bin/env bash\n")

    assert validate_template_tree(tmp_path) == ()


def test_a_tree_missing_its_thumbnail_is_flagged(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    (tmp_path / MANIFEST_THUMBNAIL_NAME).unlink()

    problems = validate_template_tree(tmp_path)

    assert any("thumbnail" in problem for problem in problems)


def test_a_tree_still_carrying_the_placeholder_thumbnail_is_flagged(
    tmp_path: Path,
) -> None:
    _write_tree(tmp_path, thumbnail_text="<!-- minds-placeholder-thumbnail -->")

    problems = validate_template_tree(tmp_path)

    assert any("placeholder thumbnail" in problem for problem in problems)


def test_a_readme_with_an_unfinished_block_is_flagged(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    (tmp_path / "README.md").write_text("<!-- FILL-IN (publishing agent): overview -->")

    problems = validate_template_tree(tmp_path)

    assert any("FILL-IN" in problem for problem in problems)


def test_every_problem_in_a_tree_is_reported_at_once(tmp_path: Path) -> None:
    # A publisher fixing one failure at a time per round-trip is the slow path;
    # the gate reports the whole set.
    _write_tree(
        tmp_path,
        markdown_text=_MINIMAL_MARKDOWN.replace("title: Slack Inbox", "title: Drifted"),
        thumbnail_text="<!-- minds-placeholder-thumbnail -->",
    )

    problems = validate_template_tree(tmp_path)

    assert len(problems) >= 2


# --- the import constraint that keeps the publish-time gate runnable ---


def test_the_schema_module_imports_only_stdlib_and_pydantic() -> None:
    """The publish-time gate runs under `uv run --no-project --with pydantic`.

    That resolves no workspace project, so any workspace import added here
    would break the gate in the worker's post-reset worktree -- where there is
    no venv -- and the failure would only surface during a real publish.
    """
    module_path = Path(template_manifest.__file__)
    tree = ast.parse(module_path.read_text())

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            imported_roots.add(node.module.split(".")[0])

    allowed = set(sys.stdlib_module_names) | {"pydantic"}
    assert imported_roots <= allowed, (
        f"non-stdlib, non-pydantic imports would break the publish-time gate: {imported_roots - allowed}"
    )


def test_activation_and_adaptation_requirements_coexist_in_one_table(
    tmp_path: Path,
) -> None:
    # The merge that removed the Prerequisites/Requirements split: both kinds of
    # item live under [requirements], and which is which is a property of the
    # entry rather than of the heading a human filed it under.
    manifest = _manifest(
        _MINIMAL_TOML
        + """
[[requirements.permission]]
scope = "slack-api"
permission = "slack-read-all"

[[requirements.adaptation]]
summary = "the digest channel is hardcoded"
resolution = "ask the user which channel to watch"
""",
        tmp_path,
    )

    assert manifest.requirements.has_activation_requirements()
    assert len(manifest.requirements.adaptation) == 1
    assert manifest.requirements.adaptation[0].summary.startswith("the digest")

    # Only the activation half is cross-checked against the markdown; the
    # adaptation entries are prose on both sides, so there is nothing to compare
    # one-for-one and their presence must not make the files "disagree".
    markdown = (
        _MINIMAL_MARKDOWN + "\n- requires_permission: slack-api / slack-read-all\n"
    )
    assert check_markdown_agreement(manifest, markdown) == ()


def test_an_template_needing_no_activation_says_so() -> None:
    # use-template branches on this: nothing to initiate means it can go
    # straight to the adaptation conversation instead of opening approval flows.
    assert not Requirements().has_activation_requirements()
    assert not Requirements(
        adaptation=({"summary": "swap the data source"},)
    ).has_activation_requirements()


# --- front matter is YAML, and titles are the user's own words ---


@pytest.mark.parametrize(
    "title",
    [
        pytest.param('The "Daily" Digest', id="embedded_quotes"),
        pytest.param("Notes: the sequel", id="colon_space"),
        pytest.param('"Leading" quote', id="leading_quote"),
        pytest.param("#hashtag start", id="leading_hash"),
        pytest.param("100", id="looks_like_a_number"),
        pytest.param("true", id="looks_like_a_bool"),
        pytest.param("back\\slash", id="backslash"),
    ],
)
def test_a_quoted_front_matter_title_matches_the_toml(
    title: str, tmp_path: Path
) -> None:
    """Generated front matter is double-quoted, so parsing must unquote it.

    These are all values a user could legitimately type as a title. Emitted
    bare, each one changes how YAML reads the line -- and the front-matter/TOML
    comparison would then report a disagreement that does not exist and fail
    the publish.
    """
    manifest = _manifest(
        _MINIMAL_TOML.replace('title = "Slack Inbox"', f"title = {json.dumps(title)}"),
        tmp_path,
    )
    markdown = (
        "---\n"
        f"title: {json.dumps(title)}\n"
        'description: "A daily digest."\n'
        'thumbnail: "template.svg"\n'
        "format: v2\n"
        "---\n\n# Heading\n"
    )

    assert check_markdown_agreement(manifest, markdown) == ()


def test_a_single_quoted_scalar_is_also_understood(tmp_path: Path) -> None:
    # YAML's other quoting style, with its doubled-quote escape.
    manifest = _manifest(
        _MINIMAL_TOML.replace('title = "Slack Inbox"', 'title = "Bob\'s Digest"'),
        tmp_path,
    )
    markdown = (
        "---\n"
        "title: 'Bob''s Digest'\n"
        "description: 'A daily digest.'\n"
        "thumbnail: 'template.svg'\n"
        "---\n\n# Heading\n"
    )

    assert check_markdown_agreement(manifest, markdown) == ()


def test_an_unquoted_plain_title_still_works(tmp_path: Path) -> None:
    # The common case must not regress: most titles need no quoting at all.
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    assert check_markdown_agreement(manifest, _MINIMAL_MARKDOWN) == ()


# --- a manifest written by a workspace we are not ---


def test_a_format_this_workspace_does_not_write_is_refused(tmp_path: Path) -> None:
    """Publishing is the only thing that parses, and it writes what it read.

    Reading a newer manifest for its recognisable parts would mean re-publishing
    a file we did not fully understand: its unknown tables are still in it, so
    stamping our own format on it yields something the next reader cannot parse,
    and stripping them deletes what its author declared.
    """
    with pytest.raises(TemplateManifestParseError) as excinfo:
        _manifest(
            _MINIMAL_TOML.replace('format = "v2"', 'format = "v3"')
            + '\n[environment.brew]\njq = "*"\n',
            tmp_path,
        )

    message = str(excinfo.value)
    assert "'v3'" in message
    assert CURRENT_MANIFEST_FORMAT in message
    assert "Update this workspace" in message


def test_the_refusal_does_not_depend_on_finding_an_unknown_key(
    tmp_path: Path,
) -> None:
    # A foreign manifest that happens to contain only fields we know is still
    # foreign: we would re-publish it under our own format having never seen
    # whatever its version actually means.
    with pytest.raises(TemplateManifestParseError):
        _manifest(_MINIMAL_TOML.replace('format = "v2"', 'format = "v3"'), tmp_path)


def test_a_manifest_with_no_format_key_is_treated_as_ours(tmp_path: Path) -> None:
    # The field defaults to the current format, so its absence means "written
    # before anyone thought to record it", not "written by a stranger".
    manifest = _manifest(_MINIMAL_TOML.replace('format = "v2"\n', ""), tmp_path)

    assert manifest.format == CURRENT_MANIFEST_FORMAT
