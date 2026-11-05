from tests.helpers import vm_id
from weekend_saver.arm import parse_resource_id
from weekend_saver.models import (
    Action,
    ActionPlan,
    ActionResult,
    Decision,
    Outcome,
    PlannedAction,
    PowerState,
    RunReport,
    RunRequest,
    RunStatus,
)
from weekend_saver.report import format_plan, format_report


def test_report_lists_every_resource_with_outcome_and_totals():
    report = RunReport(
        Action.DISABLE,
        RunStatus.COMPLETED,
        (
            ActionResult(vm_id("app"), Outcome.SUCCEEDED, "disabled", PowerState.STOPPED),
            ActionResult(vm_id("bad"), Outcome.FAILED, "azure said no", PowerState.RUNNING),
        ),
    )

    text = format_report(report)

    assert "disable" in text
    assert "FAILED" in text.splitlines()[0]
    assert f"{vm_id('bad')}: azure said no" in text
    assert "1 succeeded, 1 failed" in text


def test_invalid_report_shows_the_error():
    report = RunReport(Action.ENABLE, RunStatus.INVALID, error="no snapshot")

    text = format_report(report)

    assert "no snapshot" in text
    assert "FAILED" in text.splitlines()[0]


def test_plan_states_how_many_resources_will_change():
    target = parse_resource_id(vm_id("app"))
    idle = parse_resource_id(vm_id("idle"))
    plan = ActionPlan(
        RunRequest(Action.DISABLE, ()),
        (
            PlannedAction(target, PowerState.RUNNING, Decision.ACT, "running; will be disabled"),
            PlannedAction(idle, PowerState.STOPPED, Decision.SKIP, "already stopped"),
        ),
    )

    text = format_plan(plan)

    assert "disable 1 of 2" in text
    assert f"{vm_id('idle')}: already stopped" in text
