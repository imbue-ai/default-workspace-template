from imbue.minds_evals import flow_runner
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import FlowStartPath
from imbue.minds_evals.data_types import FlowSurface
from imbue.minds_evals.data_types import ScriptedFlowAction
from imbue.minds_evals.data_types import ScriptedFlowActionKind
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.mock_verification_agent_test import ScriptedVerificationAgent

_ORIGIN = "http://127.0.0.1:8000/"


def _check(script: tuple[ScriptedFlowAction, ...]) -> UiFlowCheck:
    return UiFlowCheck(
        check_id="ui_flow_0_f",
        name="f",
        actions="Add a task.",
        script=script,
        expect="the task is listed",
        surface=FlowSurface.ORIGIN,
        start_path=FlowStartPath(""),
    )


def test_a_scripted_flow_is_driven_by_its_own_script_even_where_a_model_agent_is_configured() -> None:
    script = (ScriptedFlowAction(kind=ScriptedFlowActionKind.CLICK, role="button", target="Add"),)

    agent = flow_runner.choose_flow_agent(_check(script), _ORIGIN, ScriptedVerificationAgent())

    assert isinstance(agent, ui_flows.ScriptVerificationAgent)
    assert (agent.script, agent.app_origin) == (script, _ORIGIN)


def test_a_scripted_flow_needs_no_model_agent() -> None:
    script = (ScriptedFlowAction(kind=ScriptedFlowActionKind.WAIT),)

    assert isinstance(flow_runner.choose_flow_agent(_check(script), _ORIGIN, None), ui_flows.ScriptVerificationAgent)


def test_a_model_driven_flow_is_driven_by_the_configured_agent() -> None:
    model_agent = ScriptedVerificationAgent()

    assert flow_runner.choose_flow_agent(_check(()), _ORIGIN, model_agent) is model_agent


def test_a_model_driven_flow_has_no_agent_when_none_is_configured() -> None:
    assert flow_runner.choose_flow_agent(_check(()), _ORIGIN, None) is None
