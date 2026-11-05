"""Run the workflow for the dashboard and narrate it as it happens.

Framework events from `workflow.run(..., stream=True)` are translated into small JSON
events (step started/finished, approval needed, run finished) and fanned out to every
connected browser. Only one run is active at a time.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_framework import Workflow

from weekend_saver.dashboard.history import RunHistory
from weekend_saver.dashboard.serialize import plan_to_json, report_to_json
from weekend_saver.errors import WeekendSaverError
from weekend_saver.executors import describe_error
from weekend_saver.models import ApprovalRequest, RunReport, RunRequest

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 500
# A plan nobody answers must not hold the dashboard (and its schedule) hostage
APPROVAL_TIMEOUT_SECONDS = 30 * 60
REMEMBERED_RESULTS = 50
SHUTDOWN_GRACE_SECONDS = 120
RESYNC: dict[str, Any] = {"type": "resync"}

_STEP_STATUS = {
    "executor_invoked": "running",
    "executor_completed": "done",
    "executor_failed": "failed",
}

Event = dict[str, Any]


class RunInProgressError(WeekendSaverError):
    """A run is already active; the dashboard runs one at a time."""


class NoPendingApprovalError(WeekendSaverError):
    """An approval was answered but no run is waiting for one."""


@dataclass
class _ActiveRun:
    run_id: str
    request: RunRequest
    trigger: str
    started_at: str
    steps: dict[str, str] = field(default_factory=dict)
    pending_plan: dict[str, Any] | None = None
    decision: asyncio.Future[bool] | None = None
    report: RunReport | None = None
    note: str = ""

    def snapshot(self) -> Event:
        return {
            "id": self.run_id,
            "action": self.request.action.value,
            "dry_run": self.request.dry_run,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "steps": dict(self.steps),
            "pending_plan": self.pending_plan,
        }


class RunManager:
    def __init__(
        self,
        workflow_factory: Callable[[], Workflow],
        history: RunHistory,
        clock: Callable[[], datetime] = datetime.now,
        approval_timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        # A workflow object keeps state between runs, so every run gets a fresh one
        self._workflow_factory = workflow_factory
        self._history = history
        self._clock = clock
        self._approval_timeout_seconds = approval_timeout_seconds
        self._active: _ActiveRun | None = None
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._results: dict[str, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue[Event]] = set()

    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def awaiting_approval(self) -> bool:
        return self._active is not None and self._active.decision is not None

    def current(self) -> Event | None:
        return self._active.snapshot() if self._active else None

    def result_of(self, run_id: str) -> dict[str, Any] | None:
        """The record of a recently finished run, or None while it is still going."""
        return self._results.get(run_id)

    def start(self, request: RunRequest, trigger: str) -> str:
        """Begin a run in the background and return its id."""
        if self._active is not None:
            raise RunInProgressError("A run is already in progress.")
        run = _ActiveRun(uuid.uuid4().hex[:12], request, trigger, self._clock().isoformat())
        self._active = run
        self._task = asyncio.create_task(self._execute(run))
        return run.run_id

    def answer(self, approved: bool, run_id: str | None = None) -> None:
        """Answer the pending approval; `run_id` guards against answering the wrong run."""
        run = self._active
        if run is None or run.decision is None or run.decision.done():
            raise NoPendingApprovalError("No run is waiting for approval.")
        if run_id is not None and run_id != run.run_id:
            raise NoPendingApprovalError("That approval belongs to a run that is no longer active.")
        run.decision.set_result(approved)

    async def wait(self) -> dict[str, Any] | None:
        """Wait for the active run (if any) and return its history record."""
        return await self._task if self._task else None

    async def shutdown(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Let a run in flight finish so it is recorded; a waiting approval is rejected."""
        if self._task is None or self._task.done():
            return
        if self.awaiting_approval:
            self.answer(False)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except TimeoutError:
            logger.error("A run was still in progress when the dashboard stopped")
            self._task.cancel()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    async def _execute(self, run: _ActiveRun) -> dict[str, Any]:
        self._publish({"type": "run_started", "run": run.snapshot()})
        error = ""
        try:
            await self._drive(run)
            if run.report is None:
                raise WeekendSaverError("The workflow ended without a report.")
        except Exception as exc:
            # The browser gets the error type; the full detail belongs in the log
            logger.exception("Run %s crashed", run.run_id)
            error = f"The run stopped unexpectedly: {describe_error(exc)}; see the log."
        record = self._record(run, error)
        self._active = None
        self._remember(record)
        try:
            self._history.append(record)
        except OSError:
            logger.exception("Could not write the run history")
        self._publish({"type": "run_finished", "run": record})
        return record

    async def _drive(self, run: _ActiveRun) -> None:
        workflow = self._workflow_factory()
        stream = workflow.run(run.request, stream=True)
        while pending := await self._consume(stream, run):
            approved = await self._wait_for_decision(run, pending[0].data)
            responses = {event.request_id: approved for event in pending}
            stream = workflow.run(responses=responses, stream=True)

    async def _consume(self, stream: Any, run: _ActiveRun) -> list[Any]:
        """Relay one stream to the browser; return the approval requests it ended on."""
        pending = []
        async for event in stream:
            if event.type in _STEP_STATUS:
                status = _STEP_STATUS[event.type]
                run.steps[event.executor_id] = status
                self._publish({"type": "step", "step": event.executor_id, "status": status})
            elif event.type == "request_info":
                pending.append(event)
            elif event.type == "output" and isinstance(event.data, RunReport):
                run.report = event.data
        return pending

    async def _wait_for_decision(self, run: _ActiveRun, request: ApprovalRequest) -> bool:
        run.pending_plan = plan_to_json(request.plan)
        run.decision = asyncio.get_running_loop().create_future()
        run.steps["approval"] = "waiting"
        self._publish({"type": "approval_required", "run_id": run.run_id, "plan": run.pending_plan})
        try:
            return await asyncio.wait_for(run.decision, timeout=self._approval_timeout_seconds)
        except TimeoutError:
            minutes = round(self._approval_timeout_seconds / 60)
            run.note = f"Nobody answered within {minutes} minutes, so the plan was rejected."
            return False
        finally:
            run.pending_plan = None
            run.decision = None

    def _record(self, run: _ActiveRun, error: str) -> dict[str, Any]:
        report = report_to_json(run.report) if run.report and not error else None
        return {
            "id": run.run_id,
            "action": run.request.action.value,
            "trigger": run.trigger,
            "dry_run": run.request.dry_run,
            "started_at": run.started_at,
            "finished_at": self._clock().isoformat(),
            "status": report["status"] if report else "crashed",
            "failed": report["failed"] if report else True,
            "error": (report["error"] or run.note) if report else error,
            "results": report["results"] if report else [],
        }

    def _remember(self, record: dict[str, Any]) -> None:
        self._results[record["id"]] = record
        while len(self._results) > REMEMBERED_RESULTS:
            del self._results[next(iter(self._results))]

    def _publish(self, event: Event) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A tab that stopped reading must not hold up the run. Drop its backlog and
                # tell it to start over from a fresh snapshot instead of going silent.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC)
