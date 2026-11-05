"""Render plans and run reports as plain text for the console and notifications."""

from collections import Counter
from collections.abc import Iterable

from weekend_saver.models import ActionPlan, ActionResult, Outcome, RunReport, RunStatus
from weekend_saver.planning import unapplied_results

_HEADLINES = {
    RunStatus.COMPLETED: "completed",
    RunStatus.DRY_RUN: "dry run, nothing changed",
    RunStatus.REJECTED: "rejected, nothing changed",
    RunStatus.INVALID: "FAILED before any change",
}


def format_plan(plan: ActionPlan) -> str:
    action = plan.request.action.value
    headline = f"Plan: {action} {len(plan.actionable)} of {len(plan.items)} resources"
    return "\n".join([headline, *_result_lines(unapplied_results(plan))])


def format_report(report: RunReport) -> str:
    headline = _HEADLINES[report.status]
    if report.failed and report.status is RunStatus.COMPLETED:
        headline = "FAILED for some resources"
    lines = [f"weekend-saver {report.action.value}: {headline}"]
    if report.error:
        lines.append(f"  error: {report.error}")
    lines.extend(_result_lines(report.results))
    if report.results:
        lines.append(f"  totals: {_totals(report.results)}")
    return "\n".join(lines)


def _result_lines(results: Iterable[ActionResult]) -> list[str]:
    return [
        f"  {result.outcome.value.upper():<9} {result.resource_id}: {result.detail}"
        for result in results
    ]


def _totals(results: Iterable[ActionResult]) -> str:
    counts = Counter(result.outcome for result in results)
    return ", ".join(f"{counts[outcome]} {outcome.value}" for outcome in Outcome if counts[outcome])
