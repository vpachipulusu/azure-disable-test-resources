"""Workflow executors: one class per step of the disable/enable pipeline.

LoadTargets -> ResolveState -> Approval -> Apply -> Verify -> Report

Any step that ends the run early sends its RunReport straight to Report, so there is
exactly one place where the workflow produces output. An approved plan is not applied
as shown: Approval sends the request back to LoadTargets so it is planned again from
the current state of Azure.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Never

from agent_framework import Executor, WorkflowContext, handler, response_handler

from weekend_saver.arm import build_targets, parse_resource_id
from weekend_saver.errors import ConfigError, StateStoreError
from weekend_saver.handlers import ResourceHandler
from weekend_saver.models import (
    Action,
    ActionPlan,
    ActionResult,
    AppliedPlan,
    ApprovalRequest,
    ApprovedPlan,
    Decision,
    Outcome,
    PlannedAction,
    PowerState,
    ResourceStatus,
    ResourceTarget,
    RunReport,
    RunRequest,
    RunStatus,
    ValidatedRequest,
)
from weekend_saver.notifier import Notifier
from weekend_saver.planning import decide, decide_unlisted, unapplied_result, unapplied_results
from weekend_saver.state_store import Snapshot, StateStore

logger = logging.getLogger(__name__)

LOAD_TARGETS_ID = "load_targets"
RESOLVE_STATE_ID = "resolve_state"
APPROVAL_ID = "approval"
APPLY_ID = "apply"
VERIFY_ID = "verify"
REPORT_ID = "report"

MAX_CONCURRENCY = 5
DEFAULT_VERIFY_ATTEMPTS = 10
DEFAULT_VERIFY_INTERVAL_SECONDS = 15.0
# Longer than a long weekend, shorter than the gap back to the previous week's disable run
DEFAULT_MAX_SNAPSHOT_AGE = timedelta(days=6)

_TARGET_STATE = {Action.DISABLE: PowerState.STOPPED, Action.ENABLE: PowerState.RUNNING}
_OVERRIDE_HINT = (
    "Point --state-file at the snapshot written by the latest disable run, or re-run with "
    "--ignore-prior-state to start every listed resource that is stopped."
)

Decider = Callable[[ResourceStatus], tuple[Decision, str]]
_ApprovalContext = WorkflowContext[RunRequest | ApprovedPlan | RunReport]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _invalid(action: Action, error: str) -> RunReport:
    return RunReport(action, RunStatus.INVALID, error=error)


def describe_error(exc: Exception) -> str:
    """Name a failure without echoing its message.

    Azure error text can carry tenant, principal and correlation IDs, and results are
    posted to chat webhooks. The full exception goes to the log instead.
    """
    code = getattr(getattr(exc, "error", None), "code", None)
    name = type(exc).__name__
    return f"{name} ({code})" if code else name


class LoadTargets(Executor):
    """Validate the user's resource list before anything talks to Azure."""

    def __init__(self, supported_types: frozenset[str]) -> None:
        super().__init__(id=LOAD_TARGETS_ID)
        self._supported_types = supported_types

    @handler
    async def load(
        self, request: RunRequest, ctx: WorkflowContext[ValidatedRequest | RunReport]
    ) -> None:
        try:
            targets = build_targets(request.resources, self._supported_types)
        except ConfigError as exc:
            await ctx.send_message(_invalid(request.action, str(exc)), target_id=REPORT_ID)
            return
        await ctx.send_message(ValidatedRequest(request, targets), target_id=RESOLVE_STATE_ID)


class ResolveState(Executor):
    """Read each resource's current state and decide what this run will do to it."""

    def __init__(
        self,
        handlers: Mapping[str, ResourceHandler],
        state_store: StateStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
        max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE,
    ) -> None:
        super().__init__(id=RESOLVE_STATE_ID)
        self._resource_handlers = handlers
        self._state_store = state_store
        self._clock = clock
        self._max_snapshot_age = max_snapshot_age

    @handler
    async def resolve(
        self, validated: ValidatedRequest, ctx: WorkflowContext[ActionPlan | RunReport]
    ) -> None:
        request = validated.request
        try:
            recorded = self._recorded_keys(request)
            unlisted = self._unlisted_targets(request, recorded, validated.targets)
        except StateStoreError as exc:
            await ctx.send_message(_invalid(request.action, str(exc)), target_id=REPORT_ID)
            return

        limiter = asyncio.Semaphore(MAX_CONCURRENCY)
        listed_plans = (
            self._plan(
                target,
                limiter,
                partial(
                    decide,
                    request.action,
                    recorded=target.key in recorded,
                    ignore_prior_state=request.ignore_prior_state,
                ),
            )
            for target in validated.targets
        )
        unlisted_plans = (
            self._plan(target, limiter, lambda status: decide_unlisted(status.power_state))
            for target in unlisted
        )
        items = await asyncio.gather(*listed_plans, *unlisted_plans)
        await ctx.send_message(ActionPlan(request, tuple(items)), target_id=APPROVAL_ID)

    def _recorded_keys(self, request: RunRequest) -> frozenset[str]:
        """Keys the snapshot says weekend-saver switched off; only enable uses them."""
        if request.action is Action.DISABLE:
            return frozenset()
        if request.ignore_prior_state:
            return self._recorded_keys_if_readable()
        snapshot = self._state_store.load()
        if snapshot is None:
            # Enabling nothing would look like success while every environment stays down
            raise StateStoreError(
                "No prior-state snapshot was found, so it is unknown which resources "
                f"weekend-saver disabled. {_OVERRIDE_HINT}"
            )
        self._require_recent(snapshot)
        return frozenset(snapshot.disabled)

    def _recorded_keys_if_readable(self) -> frozenset[str]:
        # --ignore-prior-state is the recovery path, so a broken snapshot must not block it
        try:
            snapshot = self._state_store.load()
        except StateStoreError:
            logger.warning("Ignoring an unreadable state file", exc_info=True)
            return frozenset()
        return frozenset(snapshot.disabled) if snapshot else frozenset()

    def _require_recent(self, snapshot: Snapshot) -> None:
        # If this weekend's snapshot was lost, an older one may still be lying around.
        # Trusting it would enable nothing (or the wrong things) and report success.
        try:
            age = self._clock() - datetime.fromisoformat(snapshot.last_disable_run or "")
        except (TypeError, ValueError) as exc:
            raise StateStoreError(
                "The state file does not say when the last disable run happened, so it "
                f"cannot be trusted. {_OVERRIDE_HINT}"
            ) from exc
        if age > self._max_snapshot_age:
            raise StateStoreError(
                f"The last disable run recorded in the state file was {age.days} days ago, "
                f"so the snapshot is not from this weekend. {_OVERRIDE_HINT}"
            )

    def _unlisted_targets(
        self, request: RunRequest, recorded: frozenset[str], targets: Iterable[ResourceTarget]
    ) -> tuple[ResourceTarget, ...]:
        """Resources we switched off that have since been removed from the user's list."""
        if request.action is Action.DISABLE or request.ignore_prior_state:
            return ()
        listed = {target.key for target in targets}
        try:
            unlisted = tuple(parse_resource_id(key) for key in sorted(recorded - listed))
        except ConfigError as exc:
            raise StateStoreError(f"The state file contains an invalid entry. {exc}") from exc
        unsupported = [
            target.resource_id
            for target in unlisted
            if target.resource_type not in self._resource_handlers
        ]
        if unsupported:
            raise StateStoreError(
                f"The state file records unsupported resources: {', '.join(unsupported)}"
            )
        return unlisted

    async def _plan(
        self, target: ResourceTarget, limiter: asyncio.Semaphore, decide_for: Decider
    ) -> PlannedAction:
        try:
            async with limiter:
                status = await self._resource_handlers[target.resource_type].get_status(target)
        except Exception as exc:
            # One unreadable resource must not hide the rest of the plan
            logger.exception("Could not read the state of %s", target.resource_id)
            return PlannedAction(
                target,
                PowerState.UNKNOWN,
                Decision.ERROR,
                f"could not read state: {describe_error(exc)}; see the log",
            )
        decision, reason = decide_for(status)
        return PlannedAction(target, status.power_state, decision, reason)


class Approval(Executor):
    """Pause for a human yes/no unless the run is a dry run or pre-approved."""

    def __init__(self) -> None:
        super().__init__(id=APPROVAL_ID)

    @handler
    async def review(self, plan: ActionPlan, ctx: _ApprovalContext) -> None:
        request = plan.request
        if request.dry_run:
            await self._end(plan, RunStatus.DRY_RUN, ctx)
        elif request.auto_approve or not plan.actionable:
            await ctx.send_message(ApprovedPlan(plan), target_id=APPLY_ID)
        else:
            await ctx.request_info(request_data=ApprovalRequest(plan), response_type=bool)

    @response_handler
    async def on_decision(
        self, original_request: ApprovalRequest, response: bool, ctx: _ApprovalContext
    ) -> None:
        plan = original_request.plan
        if not response:
            await self._end(plan, RunStatus.REJECTED, ctx)
            return
        # Plan again instead of applying what was shown. The answer may arrive hours
        # later from another process (resume), and the stored plan is only as
        # trustworthy as the checkpoint directory it was read back from.
        await ctx.send_message(replace(plan.request, auto_approve=True), target_id=LOAD_TARGETS_ID)

    async def _end(self, plan: ActionPlan, status: RunStatus, ctx: _ApprovalContext) -> None:
        report = RunReport(plan.request.action, status, unapplied_results(plan))
        await ctx.send_message(report, target_id=REPORT_ID)


class Apply(Executor):
    """Disable or enable every actionable resource, isolating per-resource failures."""

    def __init__(
        self,
        handlers: Mapping[str, ResourceHandler],
        state_store: StateStore,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(id=APPLY_ID)
        self._resource_handlers = handlers
        self._state_store = state_store
        self._clock = clock

    @handler
    async def apply(self, approved: ApprovedPlan, ctx: WorkflowContext[AppliedPlan]) -> None:
        plan = approved.plan
        action = plan.request.action
        if action is Action.DISABLE:
            self._record_disabled(plan.actionable)

        applied = await self._apply_in_order(action, plan.actionable)
        results = tuple(
            applied.get(item.target.key) or unapplied_result(item) for item in plan.items
        )
        to_verify = tuple(
            item.target
            for item in plan.actionable
            if applied[item.target.key].outcome is Outcome.SUCCEEDED
        )
        await ctx.send_message(AppliedPlan(action, results, to_verify), target_id=VERIFY_ID)

    def _record_disabled(self, items: Iterable[PlannedAction]) -> None:
        # Recorded before acting: if the run dies halfway, enable must still know about
        # everything that may have been switched off. Earlier entries keep their timestamp.
        # The run is stamped even when nothing needs stopping, so enable can tell that
        # this weekend's disable run did happen.
        now = self._clock().isoformat()
        earlier = self._state_store.load()
        disabled = {
            **{item.target.key: now for item in items},
            **(earlier.disabled if earlier else {}),
        }
        self._state_store.save(Snapshot(disabled, last_disable_run=now))

    async def _apply_in_order(
        self, action: Action, items: Sequence[PlannedAction]
    ) -> dict[str, ActionResult]:
        limiter = asyncio.Semaphore(MAX_CONCURRENCY)
        # Low order is disabled first and enabled last, e.g. apps (0) before their database (10)
        orders = sorted({item.target.order for item in items}, reverse=action is Action.ENABLE)
        applied: dict[str, ActionResult] = {}
        for order in orders:
            group = [item for item in items if item.target.order == order]
            results = await asyncio.gather(
                *(self._apply_one(action, item, limiter) for item in group)
            )
            applied.update({result.key: result for result in results})
        return applied

    async def _apply_one(
        self, action: Action, item: PlannedAction, limiter: asyncio.Semaphore
    ) -> ActionResult:
        target = item.target
        resource_handler = self._resource_handlers[target.resource_type]
        operation = (
            resource_handler.disable if action is Action.DISABLE else resource_handler.enable
        )
        try:
            async with limiter:
                await operation(target)
        except Exception as exc:
            # One failing resource must not stop the others from being processed
            logger.exception("%s failed for %s", action.value, target.resource_id)
            return ActionResult(
                target.resource_id,
                Outcome.FAILED,
                f"{action.value} failed: {describe_error(exc)}; see the log",
                item.current_state,
            )
        return ActionResult(
            target.resource_id, Outcome.SUCCEEDED, f"{action.value}d", _TARGET_STATE[action]
        )


class Verify(Executor):
    """Confirm each resource actually reached its target state."""

    def __init__(
        self,
        handlers: Mapping[str, ResourceHandler],
        state_store: StateStore,
        *,
        attempts: int = DEFAULT_VERIFY_ATTEMPTS,
        interval_seconds: float = DEFAULT_VERIFY_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(id=VERIFY_ID)
        self._resource_handlers = handlers
        self._state_store = state_store
        self._attempts = attempts
        self._interval_seconds = interval_seconds

    @handler
    async def verify(self, applied: AppliedPlan, ctx: WorkflowContext[RunReport]) -> None:
        limiter = asyncio.Semaphore(MAX_CONCURRENCY)
        confirmed = await asyncio.gather(
            *(self._confirm(target, applied.action, limiter) for target in applied.to_verify)
        )
        verified = {result.key: result for result in confirmed}
        results = tuple(verified.get(result.key, result) for result in applied.results)
        error = self._forget_running(results) if applied.action is Action.ENABLE else ""
        await ctx.send_message(
            RunReport(applied.action, RunStatus.COMPLETED, results, error), target_id=REPORT_ID
        )

    async def _confirm(
        self, target: ResourceTarget, action: Action, limiter: asyncio.Semaphore
    ) -> ActionResult:
        expected = _TARGET_STATE[action]
        state = PowerState.UNKNOWN
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._interval_seconds)
            try:
                async with limiter:
                    status = await self._resource_handlers[target.resource_type].get_status(target)
            except Exception:
                logger.warning(
                    "Verification read failed for %s (attempt %d of %d)",
                    target.resource_id,
                    attempt + 1,
                    self._attempts,
                    exc_info=True,
                )
                continue
            state = status.power_state
            if state is expected:
                return ActionResult(
                    target.resource_id, Outcome.SUCCEEDED, f"{action.value}d and verified", state
                )
        return ActionResult(
            target.resource_id,
            Outcome.FAILED,
            f"{action.value} was accepted but the resource is {state.value}, not {expected.value}",
            state,
        )

    def _forget_running(self, results: Iterable[ActionResult]) -> str:
        """Drop snapshot entries for resources that are up again, however they got there.

        Returns an error message instead of raising: the resources are already running,
        and losing the report now would hide that.
        """
        running = {result.key for result in results if result.state is PowerState.RUNNING}
        try:
            snapshot = self._state_store.load()
            if snapshot is None or running.isdisjoint(snapshot.disabled):
                return ""
            remaining = {
                key: value for key, value in snapshot.disabled.items() if key not in running
            }
            self._state_store.save(replace(snapshot, disabled=remaining))
        except StateStoreError as exc:
            logger.exception("Could not update the state file after enabling")
            return f"Resources were processed, but the state file was not updated: {exc}"
        return ""


class Report(Executor):
    """Notify (if configured) and emit the run report as the workflow's only output."""

    def __init__(self, notifier: Notifier | None = None) -> None:
        super().__init__(id=REPORT_ID)
        self._notifier = notifier

    @handler
    async def report(self, report: RunReport, ctx: WorkflowContext[Never, RunReport]) -> None:
        if self._notifier is not None:
            try:
                await self._notifier.notify(report)
            except Exception:
                logger.exception("Notification failed; the run report itself is unaffected")
        await ctx.yield_output(report)
