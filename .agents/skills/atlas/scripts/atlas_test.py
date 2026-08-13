"""Unit tests for the Atlas skill scripts (deterministic logic + fixed bugs).

Covers the pure functions and the two regressions found in live testing:
the live worker clobbering Evidence/footnotes, and the token-count extraction.
Run: uv run pytest .agents/skills/atlas/scripts/atlas_test.py --no-cov
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atlas_checkpoint
import atlas_config
import atlas_detect
import atlas_evidence
import atlas_generate
import atlas_index
import atlas_install_hooks
import atlas_live_refresh
import atlas_route
import atlas_summary
import atlas_topic
import atlas_transcript
import atlas_validate

# --- atlas_checkpoint.parse_interval ---------------------------------------


def test_parse_interval_units():
    assert atlas_checkpoint.parse_interval("3m") == 180
    assert atlas_checkpoint.parse_interval("90s") == 90
    assert atlas_checkpoint.parse_interval("2") == 120  # bare number = minutes


def test_parse_interval_default_and_clamp():
    assert atlas_checkpoint.parse_interval(None) == atlas_checkpoint.DEFAULT_INTERVAL_S
    assert (
        atlas_checkpoint.parse_interval("garbage")
        == atlas_checkpoint.DEFAULT_INTERVAL_S
    )
    assert atlas_checkpoint.parse_interval("1s") == 60  # clamped up to MIN
    assert atlas_checkpoint.parse_interval("99m") == 600  # clamped down to MAX


# --- atlas_checkpoint.splice_status ----------------------------------------


def test_splice_status_replaces_and_bolds():
    page = "# T\n<!-- atlas:status -->\nOLD\n<!-- /atlas:status -->\n\n## Current state\nx\n"
    out = atlas_checkpoint.splice_status(page, "RUNNING · last active 1m ago")
    assert "**RUNNING** · last active 1m ago" in out
    assert "OLD" not in out
    assert "## Current state" in out  # rest untouched


def test_splice_status_no_markers_returns_none():
    assert atlas_checkpoint.splice_status("# no markers here", "X") is None


def test_atomic_write_replaces_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "page.md"
    atlas_checkpoint.atomic_write(p, "one")
    assert p.read_text() == "one"
    atlas_checkpoint.atomic_write(p, "two")  # overwrite
    assert p.read_text() == "two"
    assert not (tmp_path / "page.md.tmp").exists()  # temp cleaned up by replace


# --- atlas_live_refresh section boundaries (the corruption regression) ------

PAGE_WITH_EVIDENCE = """# Topic
<!-- atlas:status -->
S
<!-- /atlas:status -->

## Current state
old current
## Why this exists
why
## How it got here
- a
## Decisions
- d
## Implementation shape
- f
## Open questions
- q
## Next steps
- old next step

<details>
<summary>Evidence</summary>

| id | source | quote |
|---|---|---|
| a | file | "q" |

</details>

[^a]: a footnote def
"""


def test_replace_last_section_preserves_evidence_and_footnotes():
    # Regression: replacing §7 (the last ## section) must NOT eat the Evidence
    # block or footnote definitions that follow it under no heading.
    out = atlas_live_refresh.replace_section(
        PAGE_WITH_EVIDENCE, "## Next steps", "- brand new next step"
    )
    assert "- brand new next step" in out
    assert "- old next step" not in out
    assert "<summary>Evidence</summary>" in out
    assert "[^a]: a footnote def" in out


def test_replace_middle_section_stops_at_next_heading():
    out = atlas_live_refresh.replace_section(
        PAGE_WITH_EVIDENCE, "## Current state", "new current"
    )
    assert "new current" in out
    assert "old current" not in out
    assert "## Why this exists" in out
    assert "why" in out  # the following section is intact


def test_section_body_reads_between_boundaries():
    assert atlas_live_refresh._section_body(PAGE_WITH_EVIDENCE, "## Decisions") == "- d"


# --- atlas_detect JSON parsing + slug validation ---------------------------


def test_parse_json_tolerates_fences_and_prose():
    assert atlas_detect.parse_json('{"new_topic": false}') == {"new_topic": False}
    assert atlas_detect.parse_json('```json\n{"new_topic": true}\n```') == {
        "new_topic": True
    }
    assert atlas_detect.parse_json('here: {"a": 1} ok')["a"] == 1
    assert atlas_detect.parse_json("not json at all") == {}


def test_parse_json_rejects_non_object():
    # A non-object reply (string/list/number) must degrade to {}, not reach .get().
    assert atlas_detect.parse_json('"none"') == {}
    assert atlas_detect.parse_json("[1, 2]") == {}
    assert atlas_detect.parse_json("42") == {}


def test_valid_slug():
    assert atlas_detect.valid_slug("quick-notes")
    assert not atlas_detect.valid_slug("Quick_Notes")
    assert not atlas_detect.valid_slug("-bad")
    assert not atlas_detect.valid_slug("")


def test_toml_str_escapes_quotes_and_newlines():
    assert atlas_detect._toml_str('Add "AI" summaries') == 'Add \\"AI\\" summaries'
    assert atlas_detect._toml_str("line1\nline2") == "line1 line2"
    assert atlas_detect._toml_str("back\\slash") == "back\\\\slash"


def test_write_proposal_produces_parseable_toml(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    nasty = 'Add "AI" summaries\nand tags'
    decl = atlas_detect.write_proposal(
        tmp_path, "bookmarks", "ai-tags", nasty, "why", 0.0
    )
    data = tomllib.loads(decl.read_text(encoding="utf-8"))  # must not raise
    assert data["slug"] == "ai-tags"
    assert data["project"] == "bookmarks"
    assert "AI" in data["title"]  # title survived, just escaped


# --- atlas_evidence citation parsing ---------------------------------------


def test_cited_ids():
    text = "body [^a] more\n[^a]: def a\n[^b]: def b\n"
    assert atlas_evidence._cited_ids(text) == ["a", "b"]


# --- atlas_transcript: iso parsing, keywords, word-boundary matching -------


def test_iso_to_epoch():
    assert atlas_transcript._iso_to_epoch("2026-08-11T22:37:45.000Z") > 0
    assert atlas_transcript._iso_to_epoch(None) == 0.0
    assert atlas_transcript._iso_to_epoch("nonsense") == 0.0


def test_matches_word_boundary():
    kw = {"notes"}
    assert atlas_transcript._matches(
        {"type": "user_message", "content": "some notes"}, kw
    )
    assert not atlas_transcript._matches(
        {"type": "user_message", "content": "footnotes only"}, kw
    )
    assert atlas_transcript._matches(
        {"type": "user_message", "content": "x"}, set()
    )  # no kw = all


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def test_activity_since_counts_and_scopes(tmp_path):
    f = tmp_path / "events.jsonl"
    _write_events(
        f,
        [
            {
                "type": "user_message",
                "content": "about notes",
                "timestamp": "2026-01-01T00:00:01Z",
            },
            {
                "type": "assistant_message",
                "text": "working on notes",
                "timestamp": "2026-01-01T00:00:02Z",
                "usage": {"output_tokens": 10},
            },
            {
                "type": "assistant_message",
                "text": "unrelated thing",
                "timestamp": "2026-01-01T00:00:03Z",
                "usage": {"output_tokens": 5},
            },
            {
                "type": "tool_result",
                "tool_name": "Bash",
                "output": "notes",
                "timestamp": "2026-01-01T00:00:04Z",
            },
        ],
    )
    unscoped = atlas_transcript.activity_since([f], 0.0)
    assert unscoped["turns"] == 2
    assert unscoped["tokens"] == 15
    scoped = atlas_transcript.activity_since([f], 0.0, {"notes"})
    assert scoped["turns"] == 1  # only the on-topic assistant turn
    assert scoped["tokens"] == 10
    assert scoped["tools"] == 1  # the tool_result mentioning notes


def test_status_activity_single_pass(tmp_path):
    f = tmp_path / "events.jsonl"
    _write_events(
        f,
        [
            {
                "type": "assistant_message",
                "text": "notes v1",
                "timestamp": "2026-01-01T00:00:01Z",
            },
            {
                "type": "assistant_message",
                "text": "notes v2",
                "timestamp": "2026-01-01T00:00:09Z",
            },
            {
                "type": "assistant_message",
                "text": "off-topic",
                "timestamp": "2026-01-01T00:00:10Z",
            },
        ],
    )
    t2 = atlas_transcript._iso_to_epoch("2026-01-01T00:00:05Z")
    s = atlas_transcript.status_activity([f], t2, {"notes"})
    assert s["turns_since_checkpoint"] == 1  # only the on-topic turn after t2
    assert s["last_ts"] == atlas_transcript._iso_to_epoch("2026-01-01T00:00:09Z")
    # checkpoint None => count all on-topic turns
    s_all = atlas_transcript.status_activity([f], None, {"notes"})
    assert s_all["turns_since_checkpoint"] == 2


def test_reduce_scopes_to_keywords(tmp_path):
    f = tmp_path / "events.jsonl"
    _write_events(
        f,
        [
            {
                "type": "user_message",
                "content": "build the notes board",
                "timestamp": "2026-01-01T00:00:01Z",
                "event_id": "u1",
            },
            {
                "type": "assistant_message",
                "text": "decided to use JSON for notes",
                "timestamp": "2026-01-01T00:00:02Z",
                "event_id": "a1",
            },
            {
                "type": "assistant_message",
                "text": "decided to refactor something else entirely",
                "timestamp": "2026-01-01T00:00:03Z",
                "event_id": "a2",
            },
        ],
    )
    out = atlas_transcript.reduce([f], 0.0, 4000, {"notes"})
    assert "transcript:u1" in out
    assert "transcript:a1" in out
    assert "transcript:a2" not in out  # off-topic turn excluded


# --- atlas_validate on crafted pages ---------------------------------------


def _valid_page() -> str:
    body = "\n".join(
        f"## {h}\ncontent [^c]"
        for h in [
            "Current state",
            "Why this exists",
            "How it got here",
            "Decisions",
            "Implementation shape",
            "Open questions",
            "Next steps",
        ]
    )
    return (
        "# T\n<!-- atlas:status -->\nS\n<!-- /atlas:status -->\n\n"
        + body
        + "\n\n[^c]: a source\n"
    )


def _write_page(repo_root: Path, slug: str, text: str) -> None:
    (repo_root / "atlas").mkdir(parents=True, exist_ok=True)
    (repo_root / "atlas" / f"{slug}.md").write_text(text, encoding="utf-8")


def test_validate_good_page(tmp_path):
    _write_page(tmp_path, "t", _valid_page())
    errors, _ = atlas_validate.validate(tmp_path, "t")
    assert errors == []


def test_validate_missing_section(tmp_path):
    page = _valid_page().replace("## Decisions\ncontent [^c]\n", "")
    _write_page(tmp_path, "t", page)
    errors, _ = atlas_validate.validate(tmp_path, "t")
    assert any("Decisions" in e for e in errors)


def test_validate_unresolved_citation(tmp_path):
    page = _valid_page().replace("[^c]: a source\n", "")
    _write_page(tmp_path, "t", page)
    errors, _ = atlas_validate.validate(tmp_path, "t")
    assert any("unresolved citation" in e for e in errors)


def test_validate_secret_gate(tmp_path):
    page = _valid_page().replace(
        "## Next steps\ncontent [^c]",
        '## Next steps\ntoken = "abcdef0123456789abcd" [^c]',
    )
    _write_page(tmp_path, "t", page)
    errors, _ = atlas_validate.validate(tmp_path, "t")
    assert any("secret" in e for e in errors)


def test_validate_word_cap(tmp_path):
    filler = "word " * 1200
    page = _valid_page().replace(
        "## Current state\ncontent [^c]", f"## Current state\n{filler}[^c]"
    )
    _write_page(tmp_path, "t", page)
    errors, _ = atlas_validate.validate(tmp_path, "t")
    assert any("word cap" in e for e in errors)


def test_validate_missing_page(tmp_path):
    errors, _ = atlas_validate.validate(tmp_path, "nope")
    assert errors and "missing" in errors[0]


# --- atlas_index: project grouping -----------------------------------------


def test_topic_project_defaults_to_slug():
    assert atlas_index.topic_project({}, "solo") == "solo"
    assert atlas_index.topic_project({"project": "board"}, "cards") == "board"


def test_gather_groups_features_under_projects(tmp_path):
    topics = tmp_path / "atlas" / "topics"
    topics.mkdir(parents=True)
    (topics / "cards.toml").write_text(
        'slug="cards"\ntitle="Cards"\nproject="board"\n', encoding="utf-8"
    )
    (topics / "dnd.toml").write_text(
        'slug="dnd"\ntitle="Drag and drop"\nproject="board"\n', encoding="utf-8"
    )
    (topics / "solo.toml").write_text('slug="solo"\ntitle="Solo"\n', encoding="utf-8")
    projects = atlas_index.gather(tmp_path)
    assert {f["slug"] for f in projects["board"]} == {"cards", "dnd"}  # two features
    assert [f["slug"] for f in projects["solo"]] == ["solo"]  # standalone project


# --- atlas_route: prompt-driven task routing -------------------------------


def test_route_is_trivial():
    assert atlas_route.is_trivial("yes")
    assert atlas_route.is_trivial("ok, thanks!")
    assert atlas_route.is_trivial("continue")
    assert atlas_route.is_trivial("too short")  # below the char floor
    assert not atlas_route.is_trivial(
        "Build a Kanban board app with draggable columns and cards"
    )


def test_route_prompt_from_input():
    assert (
        atlas_route._prompt_from_input('{"prompt": "do the thing"}') == "do the thing"
    )
    assert atlas_route._prompt_from_input("raw text prompt") == "raw text prompt"
    assert atlas_route._prompt_from_input("") == ""
    assert atlas_route._prompt_from_input("{bad json") == "{bad json"


def test_route_apply_verdict_creates_proposed_live_page(tmp_path, monkeypatch):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    verdict = {
        "route": "new",
        "project": "board",
        "slug": "drag-drop",
        "title": "Drag and drop",
        "why": "cards need reordering",
        "complexity": "large",
    }
    result = atlas_route.apply_verdict(tmp_path, verdict, 0.0)
    assert result["action"] == "create" and result["slug"] == "drag-drop"
    decl = tomllib.loads(
        (tmp_path / "atlas" / "topics" / "drag-drop.toml").read_text(encoding="utf-8")
    )
    assert decl["status"] == "proposed"  # human still ratifies
    assert decl["live_model"] is True  # so end-of-task full-gen picks it up
    assert (tmp_path / "atlas" / "drag-drop.md").is_file()  # skeleton page written


def test_route_apply_verdict_skips_small_task(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    verdict = {"route": "new", "slug": "tiny", "complexity": "small"}
    result = atlas_route.apply_verdict(tmp_path, verdict, 0.0)
    assert result["action"] == "skip"
    assert not (tmp_path / "atlas" / "topics" / "tiny.toml").exists()


def test_route_apply_verdict_skips_unknown_existing(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    result = atlas_route.apply_verdict(
        tmp_path, {"route": "existing", "slug": "ghost"}, 0.0
    )
    assert result["action"] == "skip"


def test_route_apply_verdict_associates_existing(tmp_path, monkeypatch):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    atlas_detect.write_proposal(tmp_path, "board", "cards", "Cards", "why", 0.0)
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")
    result = atlas_route.apply_verdict(
        tmp_path, {"route": "existing", "slug": "cards"}, 0.0
    )
    assert result["action"] == "associate" and result["agent"] == "agent-xyz"
    decl = tomllib.loads(
        (tmp_path / "atlas" / "topics" / "cards.toml").read_text(encoding="utf-8")
    )
    assert "agent-xyz" in decl["match"]["agent_ids"]


def test_route_apply_verdict_none(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    assert (
        atlas_route.apply_verdict(tmp_path, {"route": "none"}, 0.0)["action"] == "skip"
    )


def test_write_proposal_live_model_flag(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    decl = atlas_detect.write_proposal(
        tmp_path, "board", "cards", "Cards", "why", 0.0, live_model=True
    )
    data = tomllib.loads(decl.read_text(encoding="utf-8"))
    assert data["live_model"] is True
    plain = atlas_detect.write_proposal(tmp_path, "board", "dnd", "DnD", "why", 0.0)
    assert "live_model" not in tomllib.loads(plain.read_text(encoding="utf-8"))


# --- atlas_generate: background full-page generation ------------------------

_REDUCED = (
    "[USER 2026-08-11T10:00:00 transcript:evt-1]\nBuild the board\n\n"
    "[ASSISTANT 2026-08-11T10:01:00 transcript:evt-2]\nDecided React because simple\n"
)


def test_generate_build_menu():
    menu = atlas_generate.build_menu(_REDUCED)
    assert [m["id"] for m in menu] == ["t1", "t2"]
    assert menu[0]["event_id"] == "evt-1" and menu[0]["when"] == "2026-08-11"
    assert "Build the board" in menu[0]["quote"]


def test_generate_resolve_citations_strips_invented():
    menu = atlas_generate.build_menu(_REDUCED)
    sections = {
        "current_state": "works [^t1] but [^t9] is invented",
        "decisions": "chose React [^t2]",
    }
    clean, used = atlas_generate.resolve_citations(sections, menu)
    assert "[^t9]" not in clean["current_state"]  # invented marker stripped
    assert "[^t1]" in clean["current_state"]
    assert {m["id"] for m in used} == {"t1", "t2"}


def test_generate_assembled_page_validates(tmp_path):
    menu = atlas_generate.build_menu(_REDUCED)
    sections = {
        "current_state": "Board renders columns [^t1].",
        "how_it_got_here": "- 2026-08-11 — started [^t1]",
        "decisions": "- React chosen — simplicity [^t2]",
        "implementation_shape": "runner.py holds the routes [^t2]",
        "open_questions": "None.",
        "next_steps": "- ship it",
    }
    clean, used = atlas_generate.resolve_citations(sections, menu)
    page = atlas_generate.assemble_page(
        "Board",
        "**RUNNING** · active",
        "",
        "The team needs a shared board.",
        clean,
        used,
    )
    _write_page(tmp_path, "board", page)
    errors, _ = atlas_validate.validate(tmp_path, "board")
    assert errors == []


def test_generate_skips_page_with_pins(tmp_path, monkeypatch):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    atlas_detect.write_proposal(
        tmp_path, "board", "cards", "Cards", "why", 0.0, live_model=True
    )
    _write_page(
        tmp_path,
        "cards",
        "# Cards\n<!-- atlas:pinned -->\nhand edit\n<!-- /atlas:pinned -->\n",
    )
    result = atlas_generate.generate(tmp_path, "cards", 0.0, force=True)
    assert result["generated"] is False and "pinned" in result["reason"]


def test_generate_skips_when_no_transcript(tmp_path, monkeypatch):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    atlas_detect.write_proposal(
        tmp_path, "board", "cards", "Cards", "why", 0.0, live_model=True
    )
    _write_page(tmp_path, "cards", "# Cards\n\n## Current state\n\nx\n")
    result = atlas_generate.generate(tmp_path, "cards", 0.0, force=True)
    assert result["generated"] is False and "transcript" in result["reason"]


def test_generate_parse_sections_rejects_non_object():
    assert atlas_generate._parse_sections('"none"') == {}
    assert atlas_generate._parse_sections("[1, 2]") == {}
    assert atlas_generate._parse_sections('{"current_state": "x"}') == {
        "current_state": "x"
    }


def test_generate_resolves_why_markers():
    # A dangling marker copied from §2 is stripped; a valid one is kept + surfaced.
    menu = atlas_generate.build_menu(_REDUCED)
    clean, used = atlas_generate.resolve_citations(
        {"why": "because reasons [^t1] and [^t9]"}, menu
    )
    assert "[^t9]" not in clean["why"] and "[^t1]" in clean["why"]
    assert {m["id"] for m in used} == {"t1"}


def test_generate_over_token_ceiling_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    atlas_detect.write_proposal(
        tmp_path, "board", "cards", "Cards", "why", 0.0, live_model=True
    )
    _write_page(tmp_path, "cards", "# Cards\n\n## Current state\n\nx\n")
    # Log more than the ceiling's worth of full-generation tokens this hour.
    atlas_checkpoint.log_event(
        tmp_path,
        "cards",
        {
            "ts": 1000.0,
            "reason": "full_generate",
            "tokens": atlas_generate.HOURLY_TOKEN_CEILING + 1,
        },
    )
    result = atlas_generate.generate(tmp_path, "cards", 1000.0, force=False)
    assert result["generated"] is False and "ceiling" in result["reason"]


# --- atlas_checkpoint: full-generation threshold parsing --------------------


def test_fullgen_threshold_safe_parse():
    assert atlas_checkpoint.fullgen_threshold({}) == atlas_checkpoint.LARGE_TASK_TURNS
    assert atlas_checkpoint.fullgen_threshold({"full_gen_turns": 7}) == 7
    assert atlas_checkpoint.fullgen_threshold({"full_gen_turns": "5"}) == 5
    assert atlas_checkpoint.fullgen_threshold({"full_gen_turns": 0}) == 0  # "always"
    # Garbage falls back to the default rather than raising inside the checkpoint.
    assert (
        atlas_checkpoint.fullgen_threshold({"full_gen_turns": "big"})
        == atlas_checkpoint.LARGE_TASK_TURNS
    )


# --- atlas_route: debounce reservation and payload cleanup ------------------


def test_route_reserve_slot_debounces(tmp_path):
    (tmp_path / "data" / ".state" / "atlas").mkdir(parents=True)
    assert atlas_route._reserve_slot(tmp_path, "h1", 1000.0) is True
    assert atlas_route._reserve_slot(tmp_path, "h1", 1005.0) is False  # same prompt
    assert atlas_route._reserve_slot(tmp_path, "h2", 1005.0) is False  # within window
    later = 1000.0 + atlas_route.CLASSIFY_MIN_INTERVAL_S + 1
    assert atlas_route._reserve_slot(tmp_path, "h2", later) is True


def test_route_sweep_stale_payloads(tmp_path):
    d = tmp_path / "data" / ".state" / "atlas"
    d.mkdir(parents=True)
    old = d / "route-payload.OLD"
    old.write_text("x")
    os.utime(old, (1, 1))  # ancient mtime
    fresh = d / "route-payload.NEW"
    fresh.write_text("y")
    atlas_route._sweep_stale_payloads(tmp_path, 100000.0)
    assert not old.exists()  # swept
    assert fresh.exists()  # recent one kept


# --- atlas_config: feature toggles -----------------------------------------


def test_config_defaults_on(tmp_path):
    # No config file -> both features default to on.
    assert atlas_config.is_enabled(tmp_path, "pages") is True
    assert atlas_config.is_enabled(tmp_path, "summary") is True


def test_config_toggle_roundtrip(tmp_path):
    (tmp_path / "atlas").mkdir()
    atlas_config.set_enabled(tmp_path, "summary", False)
    assert atlas_config.is_enabled(tmp_path, "summary") is False
    assert atlas_config.is_enabled(tmp_path, "pages") is True  # independent
    atlas_config.set_enabled(tmp_path, "summary", True)
    assert atlas_config.is_enabled(tmp_path, "summary") is True
    # The written file is valid TOML the loader round-trips.
    with (tmp_path / "atlas" / "config.toml").open("rb") as fh:
        assert tomllib.load(fh)["summary_enabled"] is True


# --- atlas_summary: end-of-task chat summary detection ----------------------


def _fake_agent_transcript(tmp_path, monkeypatch, task_turns, prior_turns=0):
    """A temp host with one agent (ag1) whose transcript is: `prior_turns`
    assistant turns, then a user message (the current task), then `task_turns`
    assistant turns after it. Sets MNGR_HOST_DIR + MNGR_AGENT_ID."""
    host = tmp_path / "host"
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_ID", "ag1")
    tx = host / "agents" / "ag1" / "events" / "claude" / "common_transcript"
    tx.mkdir(parents=True)
    events = [
        {
            "type": "assistant_message",
            "text": f"prior {i}",
            "timestamp": "2026-08-12T00:%02d:00Z" % (i % 60),
        }
        for i in range(prior_turns)
    ]
    events.append(
        {
            "type": "user_message",
            "content": "do the thing",
            "timestamp": "2026-08-12T02:00:00Z",
        }
    )
    events += [
        {
            "type": "assistant_message",
            "text": f"did thing {i}",
            "timestamp": "2026-08-12T03:%02d:00Z" % (i % 60),
        }
        for i in range(task_turns)
    ]
    _write_events(tx / "events.jsonl", events)


def test_summary_small_task_no_nudge(tmp_path, monkeypatch):
    (tmp_path / "atlas").mkdir()
    _fake_agent_transcript(tmp_path, monkeypatch, atlas_checkpoint.LARGE_TASK_TURNS - 1)
    assert atlas_summary.check(tmp_path, 5000.0) == ""  # task below the bar


def test_summary_small_task_after_big_history_no_nudge(tmp_path, monkeypatch):
    # The regression: a small current task must NOT fire just because earlier work
    # piled up assistant turns before the latest user message.
    (tmp_path / "atlas").mkdir()
    _fake_agent_transcript(
        tmp_path,
        monkeypatch,
        task_turns=2,
        prior_turns=atlas_checkpoint.LARGE_TASK_TURNS + 10,
    )
    assert atlas_summary.check(tmp_path, 5000.0) == ""


def test_summary_fires_on_large_task_once_per_task(tmp_path, monkeypatch):
    (tmp_path / "atlas").mkdir()
    _fake_agent_transcript(tmp_path, monkeypatch, atlas_checkpoint.LARGE_TASK_TURNS + 2)
    nudge = atlas_summary.check(tmp_path, 5000.0)
    assert nudge and "<< Atlas Summary >>" in nudge
    assert "What changed" in nudge and "Open questions" in nudge
    # Same task (same last-user timestamp) -> not fired again.
    assert atlas_summary.check(tmp_path, 6000.0) == ""


def test_summary_respects_toggle(tmp_path, monkeypatch):
    (tmp_path / "atlas").mkdir()
    _fake_agent_transcript(tmp_path, monkeypatch, atlas_checkpoint.LARGE_TASK_TURNS + 2)
    atlas_config.set_enabled(tmp_path, "summary", False)
    assert atlas_summary.check(tmp_path, 5000.0) == ""  # feature off -> never fires


def test_summary_no_agent_no_nudge(tmp_path, monkeypatch):
    (tmp_path / "atlas").mkdir()
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    assert atlas_summary.check(tmp_path, 5000.0) == ""


# --- atlas_topic: lifecycle status editing ---------------------------------


def _status_of(text: str):
    return tomllib.loads(text).get("status")


def test_topic_rewrite_double_quoted_preserves_comment():
    text = 'slug = "x"\nstatus = "proposed"   # awaiting ratification\n\n[match]\n'
    out = atlas_topic.rewrite_status(text, "active")
    assert _status_of(out) == "active"
    assert "# awaiting ratification" in out


def test_topic_rewrite_single_quoted():
    out = atlas_topic.rewrite_status("status = 'proposed'\n", "shipped")
    assert _status_of(out) == "shipped"


def test_topic_rewrite_missing_status_inserts_top_level_before_table():
    text = 'slug = "x"\ntitle = "X"\n\n[match]\nagent_ids = ["a"]\n'
    out = atlas_topic.rewrite_status(text, "active")
    parsed = tomllib.loads(out)
    assert parsed.get("status") == "active"
    assert "status" not in parsed.get("match", {})  # not swallowed by the table


def test_topic_rewrite_result_always_parses():
    for text in (
        'status = "proposed"\n[match]\n',
        "status = 'proposed'\n",
        'title = "X"\n[match]\n',
    ):
        for status in atlas_topic.STATUSES:
            assert _status_of(atlas_topic.rewrite_status(text, status)) == status


def test_topic_set_status_writes_and_validates(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    decl = tmp_path / "atlas" / "topics" / "x.toml"
    decl.write_text('slug = "x"\nstatus = "proposed"\n\n[match]\n', encoding="utf-8")
    atlas_topic.set_status(tmp_path, "x", "active")
    assert _status_of(decl.read_text(encoding="utf-8")) == "active"


def test_topic_set_status_errors(tmp_path):
    (tmp_path / "atlas" / "topics").mkdir(parents=True)
    with pytest.raises(atlas_topic.UnknownStatus):
        atlas_topic.set_status(tmp_path, "x", "bogus")
    with pytest.raises(atlas_topic.TopicNotFound):
        atlas_topic.set_status(tmp_path, "ghost", "active")


# --- attribution gate: active topic + suppression --------------------------


def test_active_topic_roundtrip(tmp_path):
    assert atlas_checkpoint.active_topic(tmp_path, "ag1") is None
    atlas_checkpoint.set_active_topic(tmp_path, "ag1", "board")
    assert atlas_checkpoint.active_topic(tmp_path, "ag1") == "board"
    atlas_checkpoint.set_active_topic(tmp_path, "ag1", "auth")  # flips
    assert atlas_checkpoint.active_topic(tmp_path, "ag1") == "auth"
    assert atlas_checkpoint.active_topic(tmp_path, "other") is None


def test_suppressed_by_active(tmp_path):
    decl_shared = {"match": {"agent_ids": ["ag1"]}}
    # No agent_ids at all -> never suppressed (legacy keyword scoping).
    assert not atlas_checkpoint._suppressed_by_active(
        tmp_path, "x", {"match": {}}, ["ag1"]
    )
    # Associated but no active set yet -> not suppressed (legacy).
    assert not atlas_checkpoint._suppressed_by_active(
        tmp_path, "quick-notes", decl_shared, ["ag1"]
    )
    # Agent is actively on a different topic -> this one is suppressed.
    atlas_checkpoint.set_active_topic(tmp_path, "ag1", "atlas")
    assert atlas_checkpoint._suppressed_by_active(
        tmp_path, "quick-notes", decl_shared, ["ag1"]
    )
    # The active topic itself is not suppressed.
    assert not atlas_checkpoint._suppressed_by_active(
        tmp_path, "atlas", decl_shared, ["ag1"]
    )


def test_target_slugs_includes_agent_associated(tmp_path, monkeypatch):
    # Auto-created/tracked topics carry agent_ids and no branch; the turn-end hook
    # must still pick them up (else their pages never generate).
    topics = tmp_path / "atlas" / "topics"
    topics.mkdir(parents=True)
    (topics / "a.toml").write_text(
        'slug = "a"\nstatus = "proposed"\n\n[match]\nagent_ids = ["ag1"]\n',
        encoding="utf-8",
    )
    (topics / "b.toml").write_text(
        'slug = "b"\nstatus = "proposed"\n\n[match]\nbranches = ["main"]\n',
        encoding="utf-8",
    )
    (topics / "c.toml").write_text(
        'slug = "c"\nstatus = "shipped"\n\n[match]\nagent_ids = ["ag1"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MNGR_AGENT_ID", "ag1")
    got = set(atlas_checkpoint.target_slugs(tmp_path, None))
    assert "a" in got  # agent-associated, no branch -> now included
    assert "c" not in got  # shipped -> excluded
    assert "b" not in got  # branch-only, and tmp_path isn't that branch
    # A different agent doesn't pick up a's topic.
    monkeypatch.setenv("MNGR_AGENT_ID", "other")
    assert "a" not in set(atlas_checkpoint.target_slugs(tmp_path, None))


def test_install_hooks_adds_and_is_idempotent():
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "x/tickets.sh"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "y/nudge.sh"}]}],
        }
    }
    assert atlas_install_hooks.ensure_hooks(settings) is True

    def cmds(ev):
        return [
            h["command"]
            for g in settings["hooks"].get(ev, [])
            for h in g.get("hooks", [])
        ]

    assert any("atlas_checkpoint_hook.sh posttooluse" in c for c in cmds("PostToolUse"))
    assert any("atlas_route_hook.sh" in c for c in cmds("UserPromptSubmit"))
    assert any("tickets.sh" in c for c in cmds("UserPromptSubmit"))  # existing kept
    assert any("atlas_summary_hook.sh" in c for c in cmds("Stop"))
    assert any("nudge.sh" in c for c in cmds("Stop"))  # existing kept
    assert atlas_install_hooks.ensure_hooks(settings) is False  # idempotent


def test_install_hooks_writes_settings_file(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    assert atlas_install_hooks.install(tmp_path) is True
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "PostToolUse" in data["hooks"]
    assert atlas_install_hooks.install(tmp_path) is False  # no change second time


def test_install_scaffolds_book(tmp_path):
    # Wiring hooks without the book dir would leave the router no-op'ing forever.
    assert atlas_install_hooks.scaffold_book(tmp_path) is True
    assert (tmp_path / "atlas" / "topics").is_dir()
    assert atlas_install_hooks.scaffold_book(tmp_path) is False  # idempotent


def test_fullgen_due():
    # Router linked this task -> fire regardless of turn count.
    assert atlas_checkpoint.fullgen_due(
        route_pending=True, work_since_gen=0, threshold=12, debounced=True
    )
    # Not linked, under the bar -> no fire.
    assert not atlas_checkpoint.fullgen_due(
        route_pending=False, work_since_gen=5, threshold=12, debounced=True
    )
    # Not linked, over the bar -> fire.
    assert atlas_checkpoint.fullgen_due(
        route_pending=False, work_since_gen=12, threshold=12, debounced=True
    )
    # Debounce always wins.
    assert not atlas_checkpoint.fullgen_due(
        route_pending=True, work_since_gen=99, threshold=12, debounced=False
    )


def test_mark_generate_pending(tmp_path):
    assert atlas_checkpoint.read_state(tmp_path, "x").get("route_pending") is None
    atlas_checkpoint.mark_generate_pending(tmp_path, "x")
    assert atlas_checkpoint.read_state(tmp_path, "x")["route_pending"] is True


def test_last_user_message_ts(tmp_path):
    f = tmp_path / "events.jsonl"
    _write_events(
        f,
        [
            {
                "type": "user_message",
                "content": "first",
                "timestamp": "2026-08-12T00:00:00Z",
            },
            {
                "type": "assistant_message",
                "text": "work",
                "timestamp": "2026-08-12T00:01:00Z",
            },
            {
                "type": "user_message",
                "content": "second",
                "timestamp": "2026-08-12T02:00:00Z",
            },
        ],
    )
    ts = atlas_transcript.last_user_message_ts([f])
    assert ts == atlas_transcript._iso_to_epoch("2026-08-12T02:00:00Z")
    assert atlas_transcript.last_user_message_ts([tmp_path / "none.jsonl"]) == 0.0
