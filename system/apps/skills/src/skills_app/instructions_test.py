from pathlib import Path

from skills_app.instructions import render_instructions

_SKILL_DIRECTORY = Path("/home/user/workspace/.agents/skills/assist")


def _skill(body: str) -> str:
    return f"---\nname: assist\ndescription: Help.\n---\n\n{body}"


def test_the_body_is_rendered_and_the_frontmatter_left_out() -> None:
    html = render_instructions(_skill("# Assist\n\n- one\n- `two`\n"), _SKILL_DIRECTORY)

    assert html == "<h1>Assist</h1>\n<ul>\n<li>one</li>\n<li><code>two</code></li>\n</ul>\n"


def test_tables_are_rendered() -> None:
    html = render_instructions(_skill("| a | b |\n| - | - |\n| 1 | 2 |\n"), _SKILL_DIRECTORY)

    assert "<table>" in html
    assert "<td>2</td>" in html


def test_raw_html_and_script_links_are_not_rendered() -> None:
    html = render_instructions(
        _skill('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[run](javascript:alert(1))\n'),
        _SKILL_DIRECTORY,
    )

    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html


def test_a_link_to_a_file_beside_the_skill_opens_it_in_files() -> None:
    html = render_instructions(
        _skill("See [setup](references/set%20up.md) and [the parent](../notify-user/SKILL.md).\n"), _SKILL_DIRECTORY
    )

    assert 'href="file:///home/user/workspace/.agents/skills/assist/references/set%20up.md"' in html
    assert 'href="file:///home/user/workspace/.agents/skills/notify-user/SKILL.md"' in html


def test_a_link_to_an_absolute_path_opens_it_in_files() -> None:
    html = render_instructions(_skill("[report](/home/user/workspace/data/q4.md)\n"), _SKILL_DIRECTORY)

    assert 'href="file:///home/user/workspace/data/q4.md"' in html


def test_web_links_and_anchors_are_left_alone() -> None:
    html = render_instructions(
        _skill("[docs](https://example.com/a) [mail](mailto:a@example.com) [below](#setup)\n"), _SKILL_DIRECTORY
    )

    assert 'href="https://example.com/a"' in html
    assert 'href="mailto:a@example.com"' in html
    assert 'href="#setup"' in html
