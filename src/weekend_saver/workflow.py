"""Assemble the weekend-saver workflow graph."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from agent_framework import CheckpointStorage, Workflow, WorkflowBuilder

from weekend_saver.executors import (
    DEFAULT_MAX_SNAPSHOT_AGE,
    DEFAULT_VERIFY_ATTEMPTS,
    DEFAULT_VERIFY_INTERVAL_SECONDS,
    Apply,
    Approval,
    LoadTargets,
    Report,
    ResolveState,
    Verify,
)
from weekend_saver.handlers import ResourceHandler
from weekend_saver.notifier import Notifier
from weekend_saver.state_store import StateStore

# Checkpoints are grouped by this name; keep it stable so paused runs stay resumable
WORKFLOW_NAME = "weekend-saver"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_workflow(
    handlers: Mapping[str, ResourceHandler],
    state_store: StateStore,
    *,
    notifier: Notifier | None = None,
    checkpoint_storage: CheckpointStorage | None = None,
    verify_attempts: int = DEFAULT_VERIFY_ATTEMPTS,
    verify_interval_seconds: float = DEFAULT_VERIFY_INTERVAL_SECONDS,
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE,
    clock: Callable[[], datetime] = _utc_now,
) -> Workflow:
    """Build the graph; `handlers` maps lower-case ARM resource types to their handler."""
    load = LoadTargets(frozenset(handlers))
    resolve = ResolveState(handlers, state_store, clock=clock, max_snapshot_age=max_snapshot_age)
    approval = Approval()
    apply = Apply(handlers, state_store, clock)
    verify = Verify(
        handlers, state_store, attempts=verify_attempts, interval_seconds=verify_interval_seconds
    )
    report = Report(notifier)

    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME,
            description="Disable or enable Azure resources to save cost over the weekend",
            start_executor=load,
            checkpoint_storage=checkpoint_storage,
            output_from=[report],
        )
        .add_chain([load, resolve, approval, apply, verify, report])
        # An approved plan is planned again from current state before it is applied
        .add_edge(approval, load)
        # Early exits: invalid input, unusable snapshot, dry run, rejected plan
        .add_edge(load, report)
        .add_edge(resolve, report)
        .add_edge(approval, report)
        .build()
    )
