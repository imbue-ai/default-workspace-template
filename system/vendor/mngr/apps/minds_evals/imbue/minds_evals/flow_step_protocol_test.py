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
from imbue.minds_evals.resources.flow_step_protocol import StepRequest
from imbue.minds_evals.resources.flow_step_protocol import StepResult
from imbue.minds_evals.resources.flow_step_protocol import request_error_reason
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
