"""The secret aggregation the manifest writer does: declarations found, merged, checked
against the tree's references and the workspace's own files, and rendered into both
halves of the manifest."""

from pathlib import Path

import pytest
import write_template_manifest as writer

_SKILL_WITH_SECRETS = """---
name: fetch-widgets
description: Fetch widgets.
secrets:
  - file: widget
    variables: [WIDGET_TOKEN, WIDGET_ORG]
    note: "a Widget API token from Settings > API"
metadata:
  author: imbue
---

# Fetch widgets
"""

_APP_WITH_SECRET = """
name = "widget-app"
display_name = "Widget App"
icon = "icon.svg"

[[secrets]]
file = "widget"
variables = ["WIDGET_TOKEN"]
note = "a Widget API token"

[[secrets]]
file = "mailer"
variables = ["SMTP_PASSWORD"]
"""


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "system/apps/widget_app").mkdir(parents=True)
    (root / "system/apps/widget_app/app.toml").write_text(_APP_WITH_SECRET)
    (root / ".agents/skills/fetch-widgets").mkdir(parents=True)
    (root / ".agents/skills/fetch-widgets/SKILL.md").write_text(_SKILL_WITH_SECRETS)
    (root / "system/supervisord.conf.d").mkdir(parents=True)
    (root / "system/supervisord.conf.d/widget-app.conf").write_text(
        '[program:widget-app]\ncommand=bash -c "python3 system/scripts/with_secrets.py data/.secrets/widget.env -- widget-app"\n'
    )
    return root


def _workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "data/.secrets").mkdir(parents=True)
    for name, text in files.items():
        (workspace / "data/.secrets" / f"{name}.env").write_text(text)
    return workspace


def test_declarations_from_apps_and_skills_merge_by_file(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    declarations = writer.collect_declarations(
        root, ["system/apps/widget_app", ".agents/skills/fetch-widgets"]
    )
    assert sorted(declarations) == ["mailer", "widget"]
    widget = declarations["widget"]
    assert widget.variables == ["WIDGET_TOKEN", "WIDGET_ORG"]
    assert widget.note == "a Widget API token; a Widget API token from Settings > API"
    assert widget.sources == [
        "system/apps/widget_app/app.toml",
        ".agents/skills/fetch-widgets/SKILL.md",
    ]


def test_a_skill_outside_the_include_paths_is_not_read(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert sorted(writer.collect_declarations(root, ["system/apps/widget_app"])) == [
        "mailer",
        "widget",
    ]
    assert writer.collect_declarations(root, ["docs"]) == {}


def test_references_are_read_off_mcp_config_and_supervisord_programs(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    (root / ".mcp.template.json").write_text(
        '{"mcpServers": {"w": {"command": "python3", "args": ["system/scripts/with_secrets.py", "data/.secrets/widget.env", "--", "npx", "w"]}}}'
    )
    (root / ".mcp.json").write_text(
        '{"mcpServers": {"x": {"args": ["data/.secrets/other.env"]}}}'
    )
    assert writer.collect_references(root) == {
        "widget": [".mcp.template.json", "system/supervisord.conf.d/widget-app.conf"],
        "other": [".mcp.json"],
    }


def test_an_undeclared_reference_and_a_missing_variable_stop_the_publish(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    declarations = writer.collect_declarations(root, ["system/apps/widget_app"])
    workspace = _workspace(
        tmp_path, {"widget": "WIDGET_TOKEN='t'\n", "mailer": "OTHER='x'\n"}
    )
    problems = writer.check_declarations(
        declarations, {"widget": ["a.conf"], "ghost": ["b.conf"]}, workspace
    )
    assert len(problems) == 2
    assert any(
        "ghost" in problem and "no included app.toml" in problem for problem in problems
    )
    assert any(
        "SMTP_PASSWORD" in problem and "mailer.env" in problem for problem in problems
    )


def test_a_complete_workspace_passes_and_the_check_is_skipped_without_one(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    declarations = writer.collect_declarations(root, ["system/apps/widget_app"])
    workspace = _workspace(
        tmp_path,
        {"widget": "export WIDGET_TOKEN='t'\n", "mailer": "SMTP_PASSWORD='p'\n"},
    )
    assert (
        writer.check_declarations(
            declarations, writer.collect_references(root), workspace
        )
        == []
    )
    assert (
        writer.check_declarations(declarations, writer.collect_references(root), None)
        == []
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"file": "Widget", "variables": ["A"]},
        {"file": "widget", "variables": []},
        {"file": "widget", "variables": ["1A"]},
        {"file": "widget"},
        {"file": "widget", "variables": ["A"], "note": 3},
        "widget",
    ],
)
def test_a_malformed_declaration_is_refused(raw: object) -> None:
    with pytest.raises(writer.SecretDeclarationError):
        writer._validated_declaration(raw, "app.toml")


def test_the_skill_front_matter_parser_reads_only_the_secrets_list() -> None:
    entries = writer.parse_skill_secrets(_SKILL_WITH_SECRETS)
    assert entries == [
        {
            "file": "widget",
            "variables": ["WIDGET_TOKEN", "WIDGET_ORG"],
            "note": "a Widget API token from Settings > API",
        }
    ]
    assert writer.parse_skill_secrets("---\nname: x\ndescription: y\n---\n# x\n") == []
    assert (
        writer.parse_skill_secrets("# no front matter\nsecrets:\n  - file: x\n") == []
    )


def test_the_rendered_manifest_and_markdown_lines_agree(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    declarations = writer.collect_declarations(root, ["system/apps/widget_app"])
    rendered = writer.render_manifest(
        slug="widget",
        title="Widget",
        description="A widget.",
        version="v1",
        manifest_format="v2",
        thumbnail="template.svg",
        include=["system/apps/widget_app"],
        data_include=[],
        lineage=[],
        secret_declarations=declarations,
    )
    assert (
        '[[requirements.secret]]\nfile = "mailer"\nvariables = ["SMTP_PASSWORD"]\nnote = ""\n'
        in rendered
    )
    assert (
        '[[requirements.secret]]\nfile = "widget"\nvariables = ["WIDGET_TOKEN"]\nnote = "a Widget API token"\n'
        in rendered
    )
    lines = writer.render_requires_secret_lines(declarations)
    assert lines == (
        "- requires_secret: data/.secrets/mailer.env with SMTP_PASSWORD\n"
        "- requires_secret: data/.secrets/widget.env with WIDGET_TOKEN (a Widget API token)\n"
    )
    assert writer.render_requires_secret_lines({}) == ""


def test_main_refuses_an_undeclared_reference_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    (root / "system/supervisord.conf.d/ghost.conf").write_text(
        "command=x data/.secrets/ghost.env\n"
    )
    output = tmp_path / "template.toml"
    status = writer.main(
        [
            "--slug",
            "w",
            "--title",
            "W",
            "--description",
            "d",
            "--version",
            "v1",
            "--format",
            "v2",
            "--thumbnail",
            "template.svg",
            "--include",
            "system/apps/widget_app",
            "--repo-root",
            str(root),
            "--output",
            str(output),
        ]
    )
    assert status == 1
    assert "ghost" in capsys.readouterr().err
    assert not output.exists()
