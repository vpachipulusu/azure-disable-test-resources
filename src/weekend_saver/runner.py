"""Drive a workflow run to its report, answering approval requests along the way."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from agent_framework import CheckpointStorage, Workflow, WorkflowRunResult

from weekend_saver.errors import ConfigError, WeekendSaverError
from weekend_saver.models import ApprovalRequest, RunReport, RunRequest

Approver = Callable[[ApprovalRequest], Awaitable[bool]]

# A plan left waiting longer than this was for a different moment; make the operator start over
MAX_APPROVAL_AGE = timedelta(hours=12)


async def run_to_completion(
    workflow: Workflow, request: RunRequest, approve: Approver
) -> RunReport:
    """Run `request`, calling `approve` whenever the workflow pauses for a decision."""
    result = await workflow.run(request)
    while pending := result.get_request_info_events():
        responses = {event.request_id: await approve(event.data) for event in pending}
        result = await workflow.run(responses=responses)
    return _single_report(result)


async def resume_with_decision(
    workflow: Workflow,
    checkpoints: CheckpointStorage,
    workflow_name: str,
    *,
    approved: bool,
    now: datetime | None = None,
    max_age: timedelta = MAX_APPROVAL_AGE,
) -> RunReport:
    """Answer the approval a previous process left pending in its latest checkpoint."""
    latest = await checkpoints.get_latest(workflow_name=workflow_name)
    if latest is None or not latest.pending_request_info_events:
        raise ConfigError("No run is waiting for approval in the checkpoint directory.")

    age = (now or datetime.now(UTC)) - datetime.fromisoformat(latest.timestamp)
    if age > max_age:
        raise ConfigError(
            f"The pending approval is too old ({age.total_seconds() / 3600:.0f}h; the limit "
            f"is {max_age.total_seconds() / 3600:.0f}h). Start a fresh run instead."
        )

    responses = dict.fromkeys(latest.pending_request_info_events, approved)
    result = await workflow.run(checkpoint_id=latest.checkpoint_id, responses=responses)
    return _single_report(result)


def _single_report(result: WorkflowRunResult) -> RunReport:
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], RunReport):
        raise WeekendSaverError(f"Workflow ended without a single run report (got {outputs!r}).")
    return outputs[0]
