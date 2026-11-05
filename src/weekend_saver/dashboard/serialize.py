"""Turn workflow types into plain JSON-ready dicts for the browser and the run history."""

from typing import Any

from weekend_saver.models import ActionPlan, ActionResult, RunReport
from weekend_saver.planning import unapplied_results


def short_name(resource_id: str) -> str:
    return resource_id.rstrip("/").rsplit("/", 1)[-1]


def result_to_json(result: ActionResult) -> dict[str, Any]:
    return {
        "resource_id": result.resource_id,
        "name": short_name(result.resource_id),
        "outcome": result.outcome.value,
        "detail": result.detail,
        "state": result.state.value,
    }


def plan_to_json(plan: ActionPlan) -> dict[str, Any]:
    return {
        "action": plan.request.action.value,
        "actionable": len(plan.actionable),
        "total": len(plan.items),
        "items": [result_to_json(result) for result in unapplied_results(plan)],
    }


def report_to_json(report: RunReport) -> dict[str, Any]:
    return {
        "action": report.action.value,
        "status": report.status.value,
        "failed": report.failed,
        "error": report.error,
        "results": [result_to_json(result) for result in report.results],
    }
