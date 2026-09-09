"""Unit tests for the grade-time judge-transcript renderer. The renderer ships as a self-contained
verifier-container script under templates/tests/verifier/ (stdlib only, not a package module), so it is loaded
by file path rather than imported as ``imbue.minds_evals.templates...``."""

import json
from pathlib import Path
from typing import Any

from imbue.minds_evals.data_types import StepBoundary
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import atif_document
from imbue.minds_evals.trajectory import build_hand_built_trajectory
from imbue.minds_evals.usage import summarize_workspace_usage

_RENDERER = load_template_module("tests/verifier/render_judge_transcript.py", "minds_evals_judge_renderer")


def _sample_steps() -> list[dict[str, Any]]:
    """A workspace-shaped trajectory covering every step kind: framework-injected system steps, real
    client turns, non-empty agent messages, and tool-only agent inferences."""
    return [
        {"step_id": 1, "source": "system", "message": "WELCOME SKILL BODY -- 1738 chars of instructions"},
        {"step_id": 2, "source": "user", "message": "hi what can you do"},
        {"step_id": 3, "source": "agent", "message": ""},
        {"step_id": 4, "source": "agent", "message": "Hey! I can build you small web apps you open as a tab."},
        {
            "step_id": 5,
            "source": "agent",
            "message": "",
            "tool_calls": [{"tool_call_id": "c1", "function_name": "Skill", "arguments": {"skill": "build-app"}}],
            "observation": {"results": [{"source_call_id": "c1", "content": "Launching skill: build-app"}]},
        },
        {"step_id": 6, "source": "system", "message": "BUILD-APP SKILL BODY -- 24585 chars of instructions"},
        {"step_id": 7, "source": "agent", "message": "Here's my plan: a simple task tracker, just for you."},
        {"step_id": 8, "source": "user", "message": "Looks good, go for it."},
        {"step_id": 9, "source": "agent", "message": "Building it now."},
    ]


def test_render_keeps_only_client_turns_and_numbered_agent_messages() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_steps())

    blocks = rendered.split("\n\n")
    headers = [block.splitlines()[0] for block in blocks]
    # Two client turns and three non-empty agent messages, numbered running across the whole
    # conversation (message 2 lands before the second client turn).
    assert headers == [
        "[USER]",
        "[AGENT · message 1]",
        "[AGENT · message 2]",
        "[USER]",
        "[AGENT · message 3]",
    ]
    assert "hi what can you do" in rendered
    assert "Looks good, go for it." in rendered
    assert "Building it now." in rendered


def test_render_omits_system_steps_and_tool_only_inferences() -> None:
    rendered = _RENDERER.render_judge_transcript(_sample_steps())

    # Framework-injected text and tool plumbing carry nothing the client would see.
    assert "SKILL BODY" not in rendered
    assert "Launching skill" not in rendered
    assert "build-app" not in rendered
    assert "tool_calls" not in rendered
    assert rendered.count("[AGENT · message") == 3


def test_render_of_the_hand_built_shape_is_one_block_per_turn() -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {"step_id": 2, "source": "agent", "message": "Building it now.\n\nAll done."},
    ]

    assert (
        _RENDERER.render_judge_transcript(steps)
        == "[USER]\nBuild it\n\n[AGENT · message 1]\nBuilding it now.\n\nAll done.\n"
    )


def test_render_of_no_steps_is_empty() -> None:
    assert _RENDERER.render_judge_transcript([]) == ""


def test_load_trajectory_steps_reads_the_documents_steps(tmp_path: Path) -> None:
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps(atif_document()))

    steps = _RENDERER.load_trajectory_steps(trajectory_path)

    assert [step["source"] for step in steps] == ["user", "agent"]
    assert "[AGENT · message 1]\nBuilding it now." in _RENDERER.render_judge_transcript(steps)


def test_load_trajectory_steps_of_a_missing_or_malformed_file_is_empty(tmp_path: Path) -> None:
    assert _RENDERER.load_trajectory_steps(tmp_path / "does-not-exist.json") == []
    (tmp_path / "not-json.json").write_text("{not json")
    assert _RENDERER.load_trajectory_steps(tmp_path / "not-json.json") == []
    (tmp_path / "not-a-document.json").write_text("[1, 2]")
    assert _RENDERER.load_trajectory_steps(tmp_path / "not-a-document.json") == []


def _step_records_steps() -> list[dict[str, Any]]:
    """A conversation whose agent declares progress steps, closes one, and also opens a regular
    cross-agent ticket -- which the client never sees and the rendering must leave out."""
    return [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk create --step "Set it up"\ntk create --step "Check it works"'},
                }
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "content": "Created wor-step-aaaa: Set it up\nCreated wor-step-bbbb: Check it works",
                    }
                ]
            },
        },
        {
            "step_id": 3,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {
                    "tool_call_id": "c2",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk close wor-step-aaaa "Set the app up so it opens as a tab."'},
                }
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c2",
                        "content": (
                            "Updated wor-step-aaaa -> closed\n"
                            "tk-step wor-step-aaaa title: Set it up\n"
                            "tk-step wor-step-aaaa summary: Set the app up so it opens as a tab."
                        ),
                    }
                ]
            },
        },
        {
            "step_id": 4,
            "source": "agent",
            "message": "",
            "tool_calls": [
                {
                    "tool_call_id": "c3",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk create "crystallize todo" -t task --acceptance "branch merged"'},
                }
            ],
            "observation": {"results": [{"source_call_id": "c3", "content": "Created wor-mtrp: crystallize todo"}]},
        },
    ]


def test_render_carries_the_progress_timeline_the_client_reads() -> None:
    rendered = _RENDERER.render_judge_transcript(_step_records_steps())

    blocks = rendered.split("\n\n")
    headers = [block.splitlines()[0] for block in blocks]
    assert headers == [
        "[USER]",
        "[AGENT · message 1]",
        "[PROGRESS · step declared]",
        "[PROGRESS · step declared]",
        "[PROGRESS · step done]",
    ]
    # A closed step names the step the client watched, then the summary it closed with.
    assert "[PROGRESS · step done]\nSet it up\nSet the app up so it opens as a tab." in rendered
    assert "[PROGRESS · step declared]\nCheck it works" in rendered


def test_a_step_records_id_prefix_is_not_pinned_to_one_working_directory() -> None:
    # `tk` derives the id prefix from the directory it runs in, so a run rooted anywhere but a
    # directory called `workspace` mints `cod-step-`, `a7-step-`, and so on. Pinning `wor-` here drops
    # every declaration from such a run while its closes still render, and the criterion scores 10 for
    # the empty timeline that leaves -- a parsing break that reads as a perfect score.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk create --step "Set it up"'},
                }
            ],
            "observation": {"results": [{"source_call_id": "c1", "content": "Created cod-step-f1zl: Set it up"}]},
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS · step declared]\nSet it up" in rendered
    # A regular ticket from the same run carries no `-step-` segment and still stays out.
    assert _RENDERER.STEP_CREATED_PATTERN.findall("Created cod-f1zl: refactor the ws bridge") == []


def test_render_omits_regular_tickets_which_are_not_progress_records() -> None:
    rendered = _RENDERER.render_judge_transcript(_step_records_steps())

    # A ticket is cross-agent machinery: it carries no `tk-step` marker and its `tk create` has no
    # `--step`, so its text is not the agent's copy for the client.
    assert "crystallize todo" not in rendered
    assert "wor-mtrp" not in rendered
    assert rendered.count("[PROGRESS") == 3


def test_a_step_record_quoted_by_a_file_the_agent_read_is_not_the_agents_own_copy() -> None:
    # `tk` records only ever come back on a shell tool's output. A `Read` returns content the agent
    # asked for -- here a saved transcript quoting another run's steps -- and rendering that as this
    # agent's progress-view copy grades it on words it never wrote and the client never saw.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "Reading the old run.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Read",
                    "arguments": {"file_path": "/home/user/workspace/notes/last-run.md"},
                }
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "content": "Created cod-step-f1zl: Rebase onto mngr/crystallize-todo",
                    }
                ]
            },
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS" not in rendered
    assert "Rebase onto" not in rendered


def test_a_step_that_is_started_but_never_closed_still_reaches_the_judge() -> None:
    # `tk start` prints the `tk-step <id> title:` line, and the client sees that title on the timeline
    # from then on. Treating the line only as a label for a later close renders nothing at all for a
    # step the agent opened and left open, which is exactly the copy a stalled run is judged on.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {"tool_call_id": "c1", "function_name": "Bash", "arguments": {"command": "tk start wor-step-qa7d"}}
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "content": "Updated wor-step-qa7d -> in_progress\ntk-step wor-step-qa7d title: Set the app up so it opens as a tab",
                    }
                ]
            },
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS · step declared]\nSet the app up so it opens as a tab" in rendered


def test_a_title_captured_into_a_shell_variable_is_recovered_from_the_command() -> None:
    # `S1=$(tk create --step "...")` swallows the `Created <id>: <title>` line into the variable, so the
    # output carries the bare id alone. The workspace's own gate blesses this form, and without the
    # fallback every step declared this way is invisible to the judge.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {"command": 'S1=$(tk create --step "Set it up"); echo $S1'},
                }
            ],
            "observation": {"results": [{"source_call_id": "c1", "content": "wor-step-qa7d"}]},
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS · step declared]\nSet it up" in rendered


def test_a_title_that_cannot_be_paired_with_an_id_is_dropped_rather_than_guessed() -> None:
    # Command titles pair with output ids only by order, so a count mismatch means the mapping is a
    # guess. A ticket created in the same breath is what usually causes one, and mislabelling the
    # client's timeline with an engineer-facing ticket title is worse than rendering nothing.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {
                        "command": 'S1=$(tk create --step "Set it up"); tk create "refactor the ws bridge in apps/minds/src/ws.ts"'
                    },
                }
            ],
            "observation": {"results": [{"source_call_id": "c1", "content": "wor-step-qa7d"}]},
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS" not in rendered
    assert "ws bridge" not in rendered


def test_a_step_is_declared_once_however_many_times_its_title_is_printed() -> None:
    # `tk create` then `tk start` both name the same step. The client gets one timeline node, so a
    # second declaration would charge the agent twice for one piece of copy.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk create --step "Set it up" && tk start wor-step-qa7d'},
                }
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "content": "Created wor-step-qa7d: Set it up\nUpdated wor-step-qa7d -> in_progress\ntk-step wor-step-qa7d title: Set it up",
                    }
                ]
            },
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert rendered.count("[PROGRESS · step declared]") == 1


def test_render_shows_an_inline_image_as_a_picture_not_a_path() -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "Here's the look:\n\n![To-do app mock-up](/home/user/workspace/data/images/mock.png)\n\nGood?",
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    # The chat renders the image, so the client reads a picture -- the path is markup they never see,
    # and a judge shown the raw source scores it as a file path put in front of a non-technical client.
    assert "/home/user/workspace" not in rendered
    assert "[image: To-do app mock-up]" in rendered
    assert "Here's the look:" in rendered


def test_render_of_an_image_without_alt_text_still_says_a_picture_was_shown() -> None:
    steps = [{"step_id": 1, "source": "agent", "message": "![](/tmp/shot.png)"}]

    assert "[AGENT · message 1]\n[image]" in _RENDERER.render_judge_transcript(steps)


def test_a_subagents_own_steps_never_reach_the_clients_timeline() -> None:
    # A subagent declares steps to track its own work; the client watches only the agent it is talking
    # to. Descending into the embedded trajectory would grade the client-facing copy on titles written
    # where the client cannot see them -- and a subagent names files and branches freely.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {"command": 'tk create --step "Set it up"'},
                }
            ],
            "observation": {"results": [{"source_call_id": "c1", "content": "Created wor-step-qa7d: Set it up"}]},
            "subagent_trajectories": [
                {
                    "steps": [
                        {
                            "step_id": 1,
                            "source": "agent",
                            "message": "",
                            "tool_calls": [
                                {
                                    "tool_call_id": "s1",
                                    "function_name": "Bash",
                                    "arguments": {"command": 'tk create --step "Rebase onto mngr/crystallize-todo"'},
                                }
                            ],
                            "observation": {
                                "results": [
                                    {
                                        "source_call_id": "s1",
                                        "content": "Created wor-step-zz19: Rebase onto mngr/crystallize-todo",
                                    }
                                ]
                            },
                        }
                    ]
                }
            ],
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "Set it up" in rendered
    assert "Rebase onto" not in rendered
    assert "wor-step-zz19" not in rendered
    assert rendered.count("[PROGRESS") == 1


def test_a_ticket_opened_in_the_same_breath_as_a_step_stays_out_of_the_timeline() -> None:
    # One inference can declare a progress step and open a cross-agent ticket together. The ticket's
    # title is written for another agent -- paths, branches, jargon -- and rendering it as progress-view
    # copy would charge the agent for prose the client never saw.
    steps = [
        {"step_id": 1, "source": "user", "message": "Build it"},
        {
            "step_id": 2,
            "source": "agent",
            "message": "On it.",
            "tool_calls": [
                {
                    "tool_call_id": "c1",
                    "function_name": "Bash",
                    "arguments": {
                        "command": 'tk create --step "Set it up" && tk create "refactor the ws bridge in apps/minds/src/ws.ts" -t task'
                    },
                }
            ],
            "observation": {
                "results": [
                    {
                        "source_call_id": "c1",
                        "content": (
                            "Created wor-step-aaaa: Set it up\n"
                            "Created wor-mtrp: refactor the ws bridge in apps/minds/src/ws.ts"
                        ),
                    }
                ]
            },
        },
    ]

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "[PROGRESS · step declared]\nSet it up" in rendered
    assert "ws bridge" not in rendered
    assert rendered.count("[PROGRESS") == 1


def test_a_harness_step_boundary_never_reaches_the_judge() -> None:
    """The boundary markers the driver writes into a stepped task's trajectory are cosmetic, and the
    renderer's system-step rule is what keeps them out of what a judge scores."""
    built = build_hand_built_trajectory(
        [{"role": "user", "text": "Now change it"}, {"role": "agent", "text": "Changed."}],
        TrajectoryProvenance(
            driver_name="minds-persona-driver",
            driver_version="0.1.0",
            decider_model="claude-opus-4-8",
            decider_turns=(),
            harbor_session_id="session-1",
            case_id="todo-app",
            usage_source=UsageSource.TRANSCRIPT,
        ),
        summarize_workspace_usage(()),
        timestamp="2026-09-01T00:00:00Z",
        boundaries=(
            StepBoundary(
                name="adjust-requirements",
                started_at="2026-09-01T00:00:00Z",
                conversation_index=0,
                opening_message="Now change it",
            ),
        ),
    )

    assert built is not None
    steps = built.to_json_dict()["steps"]
    assert steps[0]["source"] == "system"

    rendered = _RENDERER.render_judge_transcript(steps)

    assert "adjust-requirements" not in rendered
    assert rendered == "[USER]\nNow change it\n\n[AGENT \u00b7 message 1]\nChanged.\n"
