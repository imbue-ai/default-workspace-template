import ast
import sys
from pathlib import Path

import pytest

from env_converge import inspiration_manifest
from env_converge.inspiration_manifest import (
    CURRENT_MANIFEST_FORMAT,
    MANIFEST_MARKDOWN_NAME,
    MANIFEST_THUMBNAIL_NAME,
    MANIFEST_TOML_NAME,
    InspirationManifest,
    InspirationManifestNotFoundError,
    InspirationManifestParseError,
    check_env_d_units,
    check_markdown_agreement,
    check_unfinished_placeholders,
    find_manifest_path,
    load_inspiration_manifest,
    validate_inspiration_tree,
)

_MINIMAL_TOML = """
format = "v2"

[inspiration]
slug = "slack-inbox"
title = "Slack Inbox"
description = "A daily digest."
thumbnail = "inspiration.svg"
version = "v1"

[recipe]
include = ["system/apps/slack_inbox"]
"""

_MINIMAL_MARKDOWN = """---
title: Slack Inbox
description: A daily digest.
thumbnail: inspiration.svg
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


def _manifest(toml_text: str, tmp_path: Path) -> InspirationManifest:
    path = tmp_path / MANIFEST_TOML_NAME
    path.write_text(toml_text)
    return load_inspiration_manifest(path)


# --- the schema itself ---


def test_a_minimal_manifest_loads_with_the_documented_defaults(tmp_path: Path) -> None:
    manifest = _manifest(_MINIMAL_TOML, tmp_path)

    assert manifest.format == CURRENT_MANIFEST_FORMAT
    assert manifest.inspiration.slug == "slack-inbox"
    assert manifest.recipe.include == ("system/apps/slack_inbox",)
    # Everything optional defaults to empty rather than requiring boilerplate:
    # most inspirations declare no environment at all, and that must stay the
    # cheap case.
    assert manifest.recipe.exclude == ()
    assert manifest.prerequisites.permission == ()
    assert manifest.prerequisites.llm is None
    assert manifest.lineage == ()
    assert manifest.environment.is_empty()


def test_the_full_environment_and_lineage_shape_round_trips(tmp_path: Path) -> None:
    manifest = _manifest(
        _MINIMAL_TOML
        + """
[prerequisites.llm]
method = "keyed"

[[prerequisites.permission]]
scope = "slack-api"
permission = "slack-read-all"

[[prerequisites.secret]]
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
    assert manifest.prerequisites.llm is not None
    assert manifest.prerequisites.llm.method == "keyed"
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

    with pytest.raises(InspirationManifestParseError):
        _manifest(toml_text, tmp_path)


def test_an_unknown_key_is_rejected_rather_than_silently_ignored(
    tmp_path: Path,
) -> None:
    # extra="forbid" is what turns a typo'd declaration into a publish-time
    # failure instead of a dependency that silently never installs.
    with pytest.raises(InspirationManifestParseError):
        _manifest(
            _MINIMAL_TOML + '\n[environment]\napt_packages = ["ripgrep"]\n', tmp_path
        )


def test_a_bad_snapshot_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InspirationManifestParseError):
        _manifest(
            _MINIMAL_TOML + '\n[environment]\napt_snapshot_timestamp = "2026-07-25"\n',
            tmp_path,
        )


def test_malformed_toml_reports_the_path_and_the_reason(tmp_path: Path) -> None:
    with pytest.raises(InspirationManifestParseError) as excinfo:
        _manifest("[inspiration\nslug =", tmp_path)

    assert MANIFEST_TOML_NAME in str(excinfo.value)
    assert "not valid TOML" in str(excinfo.value)


def test_a_missing_manifest_raises_the_not_found_error(tmp_path: Path) -> None:
    with pytest.raises(InspirationManifestNotFoundError):
        load_inspiration_manifest(tmp_path / MANIFEST_TOML_NAME)


def test_find_manifest_path_treats_absence_as_normal(tmp_path: Path) -> None:
    # An ordinary workspace has no inspiration, and a v1 inspiration repo has
    # slug-named markdown and no TOML -- neither is an error condition.
    assert find_manifest_path(tmp_path) is None

    (tmp_path / MANIFEST_TOML_NAME).write_text(_MINIMAL_TOML)
    assert find_manifest_path(tmp_path) == tmp_path / MANIFEST_TOML_NAME


def test_a_v1_repo_with_slug_named_manifests_is_not_mistaken_for_v2(
    tmp_path: Path,
) -> None:
    (tmp_path / "inspiration-slack-inbox.md").write_text(_MINIMAL_MARKDOWN)
    (tmp_path / "inspiration-slack-inbox.svg").write_text("<svg></svg>")

    assert find_manifest_path(tmp_path) is None


# --- env.d unit checks ---


def _manifest_with_units(
    units: list[str], include: list[str], tmp_path: Path
) -> InspirationManifest:
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
    # Below 2000 an inspiration's unit would interleave with the template's own
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
        + '\n[[prerequisites.permission]]\nscope = "slack-api"\npermission = "slack-read-all"\n',
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


def test_declared_prerequisites_matching_the_markdown_pass(tmp_path: Path) -> None:
    manifest = _manifest(
        _MINIMAL_TOML
        + '\n[prerequisites.llm]\nmethod = "keyed"\n'
        + '\n[[prerequisites.permission]]\nscope = "slack-api"\npermission = "slack-read-all"\n'
        + '\n[[prerequisites.secret]]\nname = "SLACK_SIGNING_SECRET"\n',
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

    assert validate_inspiration_tree(tmp_path) == ()


def test_a_tree_missing_its_thumbnail_is_flagged(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    (tmp_path / MANIFEST_THUMBNAIL_NAME).unlink()

    problems = validate_inspiration_tree(tmp_path)

    assert any("thumbnail" in problem for problem in problems)


def test_a_tree_still_carrying_the_placeholder_thumbnail_is_flagged(
    tmp_path: Path,
) -> None:
    _write_tree(tmp_path, thumbnail_text="<!-- minds-placeholder-thumbnail -->")

    problems = validate_inspiration_tree(tmp_path)

    assert any("placeholder thumbnail" in problem for problem in problems)


def test_a_readme_with_an_unfinished_block_is_flagged(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    (tmp_path / "README.md").write_text("<!-- FILL-IN (publishing agent): overview -->")

    problems = validate_inspiration_tree(tmp_path)

    assert any("FILL-IN" in problem for problem in problems)


def test_every_problem_in_a_tree_is_reported_at_once(tmp_path: Path) -> None:
    # A publisher fixing one failure at a time per round-trip is the slow path;
    # the gate reports the whole set.
    _write_tree(
        tmp_path,
        markdown_text=_MINIMAL_MARKDOWN.replace("title: Slack Inbox", "title: Drifted"),
        thumbnail_text="<!-- minds-placeholder-thumbnail -->",
    )

    problems = validate_inspiration_tree(tmp_path)

    assert len(problems) >= 2


# --- the import constraint that keeps the publish-time gate runnable ---


def test_the_schema_module_imports_only_stdlib_and_pydantic() -> None:
    """The publish-time gate runs under `uv run --no-project --with pydantic`.

    That resolves no workspace project, so any workspace import added here
    would break the gate in the worker's post-reset worktree -- where there is
    no venv -- and the failure would only surface during a real publish.
    """
    module_path = Path(inspiration_manifest.__file__)
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
