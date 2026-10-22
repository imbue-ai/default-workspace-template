import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import expect

from skills_app.mock_skill_index_searcher_test import RecordingSkillIndexSearcher
from skills_app.testing import indexed_skill
from skills_catalog.testing import commit_all
from skills_catalog.testing import write_skill
from skills_catalog.testing import write_skills_lock

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_DESKTOP = (1200, 800)
_PHONE = (390, 800)
OpenPage = Callable[[str, int, int], Page]


def _add_one_skill_of_each_kind(workspace_root: Path) -> None:
    """A skill made here, one installed, one that comes with Studio, and one of Studio's behind the scenes."""
    write_skill(workspace_root, "news-digest", "Summarize the news.", author="user")
    write_skill(workspace_root, "web-design", "Design guidelines.", author="vercel")
    write_skills_lock(workspace_root, {"web-design": "vercel-labs/agent-skills"})
    write_skill(workspace_root, "build-app", "Build an app.", author="imbue")
    write_skill(workspace_root, "handoff-summary", "Hand a chat over.", author="imbue", audience="system")
    commit_all(workspace_root, "Add one skill of each kind")


def _group(page: Page, heading: str) -> Locator:
    return page.locator(".skill-list section").filter(has=page.locator(".group-toggle", has_text=heading))


def test_the_sidebar_groups_skills_by_who_made_them_with_behind_the_scenes_folded(
    workspace_root: Path, open_skills_page: OpenPage
) -> None:
    _add_one_skill_of_each_kind(workspace_root)

    page = open_skills_page("/", *_DESKTOP)

    expect(page.locator(".group-toggle")).to_have_count(4)
    headings = page.locator(".group-toggle > span:first-child").all_inner_texts()
    assert headings == ["Made in this workspace", "Installed", "Comes with Studio", "Behind the scenes"]
    expect(_group(page, "Made in this workspace").locator("a[data-skill]")).to_have_count(3)
    expect(_group(page, "Installed").locator(".row-name")).to_have_text(["web-design"])
    expect(_group(page, "Comes with Studio").locator(".row-name")).to_have_text(["build-app"])
    expect(_group(page, "Behind the scenes").locator(".group-count")).to_have_text("1")
    expect(_group(page, "Behind the scenes").locator("a[data-skill]")).to_have_count(0)

    _group(page, "Behind the scenes").locator(".group-toggle").click()

    expect(_group(page, "Behind the scenes").locator(".row-name")).to_have_text(["handoff-summary"])


def test_a_skill_s_page_shows_who_made_it_and_its_instructions_without_repeating_its_name(
    workspace_root: Path, open_skills_page: OpenPage
) -> None:
    _add_one_skill_of_each_kind(workspace_root)

    page = open_skills_page("/news-digest", *_DESKTOP)

    expect(page.locator("article .skill-head h1")).to_have_text("news-digest")
    expect(page.locator(".chips .chip").first).to_have_text("Made by user")
    expect(page.locator(".chips .chip").nth(1)).to_contain_text("Updated")
    expect(page.locator(".skill-section p").first).to_have_text("Summarize the news.")
    # The SKILL.md opens with "# news-digest", which the page already shows as its title.
    expect(page.locator(".instructions")).to_have_text("Do the thing.")
    expect(page.locator('a[data-skill="news-digest"]')).to_have_attribute("aria-current", "page")


def test_the_open_skill_s_row_leads_back_to_the_empty_pane(open_skills_page: OpenPage) -> None:
    page = open_skills_page("/assist", *_DESKTOP)
    origin = page.url.removesuffix("/assist")

    page.locator('a[data-skill="assist"]').click()

    expect(page).to_have_url(f"{origin}/")
    expect(page.locator(".intro-lead")).to_have_text("Skills are what your agents know how to do.")
    expect(page.locator(".intro .add-button")).to_have_attribute("href", "/?find=")


def test_a_search_filters_the_list_and_ends_with_a_way_to_search_online(open_skills_page: OpenPage) -> None:
    page = open_skills_page("/", *_DESKTOP)
    expect(page.locator("a[data-skill]")).to_have_count(2)

    page.locator("#search").fill("notify")

    expect(page.locator(".row-name")).to_have_text(["notify-user"])
    expect(page.locator("a.find-online")).to_have_attribute("href", "/?find=notify")

    # Too short to search online, and matching nothing here.
    page.locator("#search").fill("z")

    expect(page.locator("a[data-skill]")).to_have_count(0)
    expect(page.locator(".skill-list .empty")).to_have_text("No skills match.")

    page.locator("#search").fill("nothing like this")

    expect(page.locator("a[data-skill]")).to_have_count(0)
    expect(page.locator("a.find-online")).to_have_text("Search online for “nothing like this”")


def test_add_a_skill_marks_each_result_against_the_workspace_s_skills(
    workspace_root: Path, searcher: RecordingSkillIndexSearcher, open_skills_page: OpenPage
) -> None:
    _add_one_skill_of_each_kind(workspace_root)
    searcher.matches = (
        indexed_skill("vercel-labs/agent-skills", "web-design", installs=900),
        indexed_skill("someone/skills", "assist", installs=50),
        indexed_skill("anthropics/skills", "pdf", installs=214612),
    )

    page = open_skills_page("/?find=design", *_DESKTOP)

    expect(page.locator(".online-skills li")).to_have_count(3)
    expect(page.locator(".find-source")).to_have_text("From skills.sh, most installed first")
    expect(page.locator(".online-name")).to_have_text(["web-design", "assist", "pdf"])
    expect(page.locator(".online-name").nth(2)).to_have_attribute("href", "https://skills.sh/anthropics/skills/pdf")
    expect(page.locator(".online-skills li").nth(0).locator("a.chip")).to_have_text("Installed")
    expect(page.locator(".online-skills li").nth(0).locator("a.chip")).to_have_attribute("href", "/web-design")
    expect(page.locator(".online-skills li").nth(1).locator(".chip").last).to_have_text("Name in use")
    expect(page.locator(".online-skills li").nth(2).locator(".chip")).to_have_text("214.6K installs")
    expect(page.locator("#new-skill")).to_have_attribute("aria-current", "page")
    assert searcher.queries == ["design"]


def test_clearing_add_a_skill_s_field_goes_back_to_its_suggestions(
    searcher: RecordingSkillIndexSearcher, open_skills_page: OpenPage
) -> None:
    searcher.matches = (indexed_skill("anthropics/skills", "pdf", installs=214612),)
    page = open_skills_page("/?find=pdf", *_DESKTOP)
    expect(page.locator(".online-skills li")).to_have_count(1)

    page.locator(".find-search input").fill("")

    expect(page.locator(".suggestion")).to_have_text(["pdf", "design", "testing", "git", "slack", "docs"])
    expect(page.locator(".online-skills")).to_have_count(0)
    expect(page).to_have_url(page.url.split("?")[0] + "?find=")


def test_add_a_skill_says_when_skills_sh_cannot_be_reached(
    searcher: RecordingSkillIndexSearcher, open_skills_page: OpenPage
) -> None:
    searcher.is_unreachable = True

    page = open_skills_page("/?find=pdf", *_DESKTOP)

    expect(page.locator(".find-status.error")).to_contain_text("Couldn't reach skills.sh")
    expect(page.locator("#find-results button", has_text="Try again")).to_be_visible()


def test_a_search_still_waiting_on_skills_sh_draws_its_answer_when_the_skills_change(
    workspace_root: Path, searcher: RecordingSkillIndexSearcher, open_skills_page: OpenPage
) -> None:
    searcher.is_unreachable = True
    searcher.hold_answers()
    page = open_skills_page("/?find=pdf", *_DESKTOP)
    expect(page.locator("a[data-skill]")).to_have_count(2)
    expect(page.locator(".find-status")).to_have_text("Searching skills.sh…")

    # The skills change while the search waits, and the page rereads them, as it does on focus.
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('a[data-skill="news-digest"]')).to_have_count(1)
    searcher.release_answers()

    expect(page.locator(".find-status.error")).to_contain_text("Couldn't reach skills.sh")
    expect(page.locator("#find-results button", has_text="Try again")).to_be_visible()


def test_a_skill_removed_while_its_page_is_open_closes_the_page(
    workspace_root: Path, open_skills_page: OpenPage
) -> None:
    write_skill(workspace_root, "draft-skill", "Not saved yet.")
    page = open_skills_page("/draft-skill", *_DESKTOP)
    expect(page.locator("article .skill-head h1")).to_have_text("draft-skill")

    shutil.rmtree(workspace_root / ".agents/skills/draft-skill")
    # The page rereads the skills whenever its window takes focus, as it does every ten seconds.
    page.evaluate("window.dispatchEvent(new Event('focus'))")

    expect(page.locator(".intro-lead")).to_be_visible()
    expect(page.locator('a[data-skill="draft-skill"]')).to_have_count(0)


def test_a_failed_reread_of_the_open_skill_keeps_it_showing(workspace_root: Path, open_skills_page: OpenPage) -> None:
    write_skill(workspace_root, "draft-skill", "Not saved yet.")
    page = open_skills_page("/draft-skill", *_DESKTOP)
    expect(page.locator(".skill-section p").first).to_have_text("Not saved yet.")
    page.route(
        re.compile(r".*/api/skills/draft-skill(/instructions)?$"),
        lambda route: route.fulfill(status=500, json={"detail": "The workspace is restarting."}),
    )

    # The edit changes the skill's entry in the list, so the page rereads the skill, and that fails.
    write_skill(workspace_root, "draft-skill", "Edited.")
    with page.expect_console_message(lambda message: "could not reread the skill" in message.text):
        page.evaluate("window.dispatchEvent(new Event('focus'))")

    expect(page.locator("article .skill-head h1")).to_have_text("draft-skill")
    expect(page.locator(".skill-section p").first).to_have_text("Not saved yet.")
    expect(page.locator("#reader .error")).to_have_count(0)


def test_an_address_with_a_malformed_escape_names_no_skill(open_skills_page: OpenPage) -> None:
    page = open_skills_page("/50%", *_DESKTOP)

    expect(page.locator("#reader .error")).to_contain_text("There is no skill called '50%'")
    expect(page.locator("a[data-skill]")).to_have_count(2)


def test_a_narrow_window_shows_the_list_or_one_skill(open_skills_page: OpenPage) -> None:
    listing = open_skills_page("/", *_PHONE)
    expect(listing.locator("a[data-skill]")).to_have_count(2)
    expect(listing.locator("#reader")).to_be_hidden()

    skill = open_skills_page("/assist", *_PHONE)
    expect(skill.locator(".sidebar")).to_be_hidden()
    expect(skill.locator("article .back")).to_have_attribute("href", "/")
    expect(skill.locator("article .skill-head h1")).to_have_text("assist")


def test_outside_the_shell_the_buttons_that_need_it_are_hidden(open_skills_page: OpenPage) -> None:
    page = open_skills_page("/assist", *_DESKTOP)
    expect(page.locator("article .skill-head h1")).to_have_text("assist")

    # The folder link, and the trash button a skill made in the workspace has.
    expect(page.locator(".skill-head .icon-button")).to_have_count(2)
    expect(page.locator(".skill-head .icon-button").nth(0)).to_be_hidden()
    expect(page.locator(".skill-head .icon-button").nth(1)).to_be_hidden()
    expect(page.locator(".skill-head .text-button")).to_be_hidden()
    expect(page.locator("#new-skill")).to_be_visible()


def test_inside_the_shell_add_a_skill_stays_on_its_search(
    searcher: RecordingSkillIndexSearcher, open_framed_skills_page: Callable[[str], Page]
) -> None:
    searcher.matches = (indexed_skill("anthropics/skills", "pdf", installs=214612),)

    shell = open_framed_skills_page("/?find=pdf")

    frame = shell.frame_locator("#app")
    shell.wait_for_function("window.messages.some((m) => m.type === 'shell:location')")
    expect(frame.locator(".online-name")).to_have_text(["pdf"])
    expect(frame.locator("article .skill-head h1")).to_have_text("Add a skill")
    expect(frame.locator(".intro")).to_have_count(0)
    assert {"type": "shell:location", "path": "/?find=pdf", "title": "Add a skill - Skills"} in shell.evaluate(
        "window.messages"
    )


def test_inside_the_shell_a_skill_s_buttons_hand_it_to_files_and_the_chat(
    workspace_root: Path, open_framed_skills_page: Callable[[str], Page]
) -> None:
    write_skill(workspace_root, "news-digest", "Summarize the news.", author="user")
    commit_all(workspace_root, "Crystallize news-digest")
    shell = open_framed_skills_page("/news-digest")
    frame = shell.frame_locator("#app")
    expect(frame.locator("article .skill-head h1")).to_have_text("news-digest")
    skill_folder = (workspace_root / ".agents/skills/news-digest").absolute()

    frame.locator(".skill-head a.icon-button").click()
    frame.locator('.skill-head button[aria-label="Remove this skill"]').click()
    frame.locator(".skill-head .text-button", has_text="Try it").click()
    frame.locator('.section-head button[aria-label="Edit SKILL.md in Files"]').click()

    shell.wait_for_function("window.messages.filter((m) => m.type !== 'shell:location').length >= 5")
    sent = shell.evaluate("window.messages.filter((m) => !['shell:location', 'shell:capabilities'].includes(m.type))")
    assert sent == [
        {"type": "shell:open-link", "url": f"file://{skill_folder}"},
        {"type": "shell:draft-text", "text": "Remove the news-digest skill from this workspace."},
        {"type": "shell:draft-text", "text": "/news-digest "},
        {"type": "shell:message", "message": {"path": f"{skill_folder}/SKILL.md", "type": "edit:file"}},
    ]
    assert {"type": "shell:location", "path": "/news-digest", "title": "news-digest - Skills"} in shell.evaluate(
        "window.messages"
    )
