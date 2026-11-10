from about_me.profile import cited_urls
from about_me.profile import fact_id
from about_me.profile import parse_fact_line
from about_me.profile import parse_profile
from about_me.testing import SAMPLE_PROFILE


def test_the_sample_profile_lists_every_body_fact_line_in_order_and_nothing_else() -> None:
    lines = parse_profile(SAMPLE_PROFILE)

    assert [line.index for line in lines] == [8, 9, 10, 11, 12]
    assert [line.fact.text for line in lines] == [
        "Designs typefaces in Berlin",
        "Writes a newsletter about type",
        "Teaches a weekend lettering class",
        "Is learning Japanese",
        "Something written by hand, with no link",
    ]
    # The frontmatter's bullet-looking line and the closing sentence are not facts.
    assert all("frontmatter" not in line.fact.text for line in lines)
    assert all("web lookup you asked for" not in line.fact.text for line in lines)


def test_a_fact_from_the_web_carries_its_source_and_date() -> None:
    raw = b"- Designs typefaces in Berlin ([Portfolio](https://example.com/portfolio)) <!-- found 2026-11-01 -->\n"

    fact = parse_fact_line(8, raw)

    assert fact.id == fact_id(8, raw)
    assert fact.text == "Designs typefaces in Berlin"
    assert fact.source_title == "Portfolio"
    assert fact.source_url == "https://example.com/portfolio"
    assert fact.found_on == "2026-11-01"
    assert fact.is_told_by_user is False
    assert fact.is_parsed is True


def test_a_source_url_with_parentheses_is_kept_whole() -> None:
    fact = parse_fact_line(
        0, b"- Has a page ([Wiki](https://en.wikipedia.org/wiki/Foo_(bar))) <!-- found 2026-01-02 -->"
    )

    assert fact.source_url == "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert fact.text == "Has a page"


def test_a_fact_the_user_shared_is_marked_and_has_no_link() -> None:
    fact = parse_fact_line(3, b"- Is learning Japanese (you told me) <!-- found 2026-11-03 -->\r\n")

    assert fact.text == "Is learning Japanese"
    assert fact.is_told_by_user is True
    assert fact.source_url is None
    assert fact.source_title is None
    assert fact.found_on == "2026-11-03"
    assert fact.is_parsed is True


def test_a_line_in_neither_shape_is_one_fact_shown_as_written() -> None:
    for raw, text in (
        (b"- Something written by hand, with no link\n", "Something written by hand, with no link"),
        (b"- Missing its date ([Site](https://example.com))\n", "Missing its date ([Site](https://example.com))"),
        (
            b"- A script link ([x](javascript:alert(1))) <!-- found 2026-01-01 -->\n",
            "A script link ([x](javascript:alert(1))) <!-- found 2026-01-01 -->",
        ),
        (b"- \n", ""),
    ):
        fact = parse_fact_line(0, raw)
        assert fact.is_parsed is False, raw
        assert fact.text == text
        assert fact.source_url is None
        assert fact.found_on is None


def test_the_id_changes_with_the_line_or_its_place() -> None:
    raw = b"- A fact (you told me) <!-- found 2026-11-03 -->\n"

    assert fact_id(4, raw) == fact_id(4, raw)
    assert fact_id(4, raw) != fact_id(5, raw)
    assert fact_id(4, raw) != fact_id(4, raw.replace(b"A fact", b"A fakt"))


def test_a_profile_without_frontmatter_reads_every_bullet_line() -> None:
    lines = parse_profile(b"- One (you told me) <!-- found 2026-01-01 -->\nA sentence.\n- Two\n")

    assert [line.index for line in lines] == [0, 2]


def test_an_unclosed_frontmatter_still_shows_the_bullet_lines_after_the_fence() -> None:
    lines = parse_profile(b"---\nname: x\n- A loose line\n")

    assert [line.fact.text for line in lines] == ["A loose line"]


def test_an_empty_or_fact_free_profile_has_no_facts() -> None:
    assert parse_profile(b"") == ()
    assert parse_profile(b"---\nname: x\n---\n\nThese facts came from a lookup.\n") == ()


def test_a_profile_that_is_not_utf8_is_still_read() -> None:
    lines = parse_profile(b"- Caf\xe9 owner (you told me) <!-- found 2026-01-01 -->\n")

    assert len(lines) == 1
    assert lines[0].fact.is_told_by_user is True


def test_cited_urls_reads_every_link_whether_or_not_the_line_is_in_the_shape() -> None:
    lines = parse_profile(
        b"- A ([P](https://a.example)) <!-- found 2026-01-01 -->\n"
        b"- Loose, see [here](https://b.example) and [there](https://c.example)\n"
        b"- Told (you told me) <!-- found 2026-01-01 -->\n"
    )

    assert cited_urls(lines) == frozenset({"https://a.example", "https://b.example", "https://c.example"})
