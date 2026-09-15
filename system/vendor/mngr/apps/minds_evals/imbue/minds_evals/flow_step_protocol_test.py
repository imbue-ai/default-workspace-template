"""The boundary the driver and the step script meet at, tested from the driver's side.

The step script itself cannot be imported here -- it needs playwright, which lives in the box's
venv and not in this project -- so what is pinned is the contract both sides validate against.
"""

import pytest
from pydantic import ValidationError

from imbue.minds_evals.resources.flow_step_protocol import REASON_STEP_ERROR
from imbue.minds_evals.resources.flow_step_protocol import REASON_UNKNOWN_ACTION
from imbue.minds_evals.resources.flow_step_protocol import StepAction
from imbue.minds_evals.resources.flow_step_protocol import StepActionKind
from imbue.minds_evals.resources.flow_step_protocol import StepCookie
from imbue.minds_evals.resources.flow_step_protocol import StepReaction
from imbue.minds_evals.resources.flow_step_protocol import StepRequest
from imbue.minds_evals.resources.flow_step_protocol import StepResult
from imbue.minds_evals.resources.flow_step_protocol import request_error_reason
from imbue.minds_evals.resources.flow_step_protocol import snapshot_line_for_ref
from imbue.minds_evals.resources.flow_step_protocol import snapshot_line_role
from imbue.minds_evals.testing import FAKE_WORKSPACE_AGENT_ID


def _request(cookie: StepCookie | None = None) -> StepRequest:
    return StepRequest(
        cdp_endpoint="http://127.0.0.1:9333",
        screenshot_path="/logs/agent/verification/flows/f/step_001.png",
        action=StepAction(kind=StepActionKind.CLICK, role="button", target="Add"),
        cookie=cookie,
    )


def test_a_request_survives_the_round_trip_it_actually_makes() -> None:
    # The driver serialises, the box deserialises: what the step performs has to be what was asked
    # for, down to the action's own fields.
    restored = StepRequest.model_validate_json(_request().model_dump_json())

    assert restored.action.kind is StepActionKind.CLICK
    assert (restored.action.role, restored.action.target) == ("button", "Add")
    assert restored.cookie is None


def test_the_cookie_travels_with_the_scope_and_flags_the_proxy_needs() -> None:
    # Scope and flags have to survive the trip to the box intact: this is what the browser is armed
    # with, and a cookie that arrives shaped differently is one the proxy will not honour.
    domain = ".{}.localhost".format(FAKE_WORKSPACE_AGENT_ID)
    cookie = StepCookie(name="mngr_forward_session", value="tok", domain=domain)

    restored = StepRequest.model_validate_json(_request(cookie=cookie).model_dump_json())

    assert restored.cookie is not None
    assert (restored.cookie.domain, restored.cookie.path) == (domain, "/")
    assert (restored.cookie.is_secure, restored.cookie.is_http_only, restored.cookie.same_site) == (True, True, "None")


def test_a_cookie_with_no_domain_is_refused_before_it_ships() -> None:
    # Playwright would reject it in the box, and that rejection would be recorded against the flow
    # as an instrument failure; refusing it at construction keeps a harness bug a harness bug.
    with pytest.raises(ValidationError) as caught:
        StepCookie(name="mngr_forward_session", value="tok", domain="")

    assert tuple(caught.value.errors()[0]["loc"]) == ("domain",)


def test_an_action_kind_the_script_cannot_perform_fails_at_the_boundary() -> None:
    # Better here, naming the field, than deep in the step where the missing branch would surface
    # as something the app appeared to do.
    with pytest.raises(ValidationError) as caught:
        StepRequest.model_validate_json(
            '{"cdp_endpoint": "x", "screenshot_path": "y", "action": {"kind": "teleport"}}'
        )

    assert tuple(caught.value.errors()[0]["loc"]) == ("action", "kind")
    assert request_error_reason(caught.value) == REASON_UNKNOWN_ACTION


def test_a_request_malformed_anywhere_else_is_the_executor_failing() -> None:
    # An unknown kind is the two sides' vocabularies drifting; anything else means the executor was
    # never handed a step it could run, which is a different thing to record.
    with pytest.raises(ValidationError) as caught:
        StepRequest.model_validate_json('{"screenshot_path": "y", "action": {"kind": "click"}}')

    assert request_error_reason(caught.value) == REASON_STEP_ERROR


def test_a_result_carries_the_page_even_when_the_step_failed() -> None:
    # What the app showed when the action did not land is the most useful thing a flow records, and
    # the grade-time judge rules on the flow's `expect` from it.
    result = StepResult.model_validate_json(
        '{"is_ok": false, "reason": "action_timed_out", "detail": "Timeout 15000ms exceeded",'
        ' "url": "https://app/", "title": "Todo", "snapshot": "- heading \\"Things to do\\""}'
    )

    assert (result.is_ok, result.reason) == (False, "action_timed_out")
    assert "Things to do" in result.snapshot
    # A capture that never happened says so rather than naming a frame nobody wrote.
    assert result.screenshot_path == ""
    # An action that failed never got as far as watching the page, and says so.
    assert result.reaction is StepReaction.UNOBSERVED


def test_a_wait_travels_as_its_own_kind_and_needs_no_target() -> None:
    restored = StepRequest.model_validate_json(
        StepRequest(
            cdp_endpoint="http://127.0.0.1:9333",
            screenshot_path="/logs/agent/verification/flows/f/step_002.png",
            action=StepAction(kind=StepActionKind.WAIT),
        ).model_dump_json()
    )

    assert restored.action.kind is StepActionKind.WAIT
    assert (restored.action.role, restored.action.target) == ("", "")


def test_a_reaction_the_driver_does_not_know_fails_at_the_boundary() -> None:
    # The step script and the driver share the vocabulary; a word outside it is the two having
    # drifted apart, which must not be read as any particular thing the page did.
    with pytest.raises(ValidationError) as caught:
        StepResult.model_validate_json('{"is_ok": true, "reaction": "exploded"}')

    assert tuple(caught.value.errors()[0]["loc"]) == ("reaction",)


def test_an_action_by_ref_survives_the_round_trip() -> None:
    restored = StepRequest.model_validate_json(
        StepRequest(
            cdp_endpoint="http://127.0.0.1:9333",
            screenshot_path="/logs/agent/verification/flows/f/step_003.png",
            action=StepAction(kind=StepActionKind.CLICK, role="checkbox", ref="e9"),
        ).model_dump_json()
    )

    assert (restored.action.role, restored.action.target, restored.action.ref) == ("checkbox", "", "e9")


def test_a_ref_without_the_role_it_was_read_on_fails_at_the_boundary() -> None:
    # The role is the only thing a ref can be checked against on the page as it now stands, so the
    # two travel together or the request is not one the step script can safely run.
    with pytest.raises(ValidationError):
        StepAction(kind=StepActionKind.CLICK, ref="e9")


def test_a_ref_that_is_not_one_the_snapshot_printed_fails_at_the_boundary() -> None:
    # The step script puts the ref into a selector, so what a ref may look like is the boundary's to
    # say rather than something the driver alone is trusted to have checked.
    with pytest.raises(ValidationError):
        StepAction(kind=StepActionKind.CLICK, role="checkbox", ref="the third one")


_SNAPSHOT = """- generic [ref=e1]:
  - textbox "New task" [ref=e4]
  - list [ref=e6]:
    - listitem [ref=e7]:
      - checkbox [ref=e8] [cursor=pointer]
      - generic [ref=e9]: buy milk
      - 'button "Delete \\"buy milk\\""' [ref=e10]
"""


@pytest.mark.parametrize(
    ("ref", "role"),
    [("e8", "checkbox"), ("e9", "generic"), ("e10", "button"), ("e4", "textbox")],
)
def test_a_snapshot_line_is_found_by_its_ref_and_read_for_its_role(ref: str, role: str) -> None:
    # The step script checks a ref against a fresh snapshot before acting on it, so the role has to
    # be read off every shape of line the "ai" rendering prints: bare, with a name, and quoted whole.
    line = snapshot_line_for_ref(_SNAPSHOT, ref)

    assert "[ref={}]".format(ref) in line
    assert snapshot_line_role(line) == role


def test_a_ref_the_snapshot_does_not_carry_finds_no_line() -> None:
    assert snapshot_line_for_ref(_SNAPSHOT, "e1000") == ""
    # A ref is matched whole, so `e1` finds its own line rather than `e10`'s.
    assert snapshot_line_for_ref(_SNAPSHOT, "e1") == "- generic [ref=e1]:"
    assert snapshot_line_role("") == ""
