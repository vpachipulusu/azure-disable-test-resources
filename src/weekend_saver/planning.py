"""Pure decision rules: given a resource's state, should this run touch it?"""

from weekend_saver.models import (
    Action,
    ActionPlan,
    ActionResult,
    Decision,
    Outcome,
    PlannedAction,
    PowerState,
    ResourceStatus,
)

SKIP_TAG = "weekend-saver"
SKIP_TAG_VALUE = "skip"


def decide(
    action: Action,
    status: ResourceStatus,
    *,
    recorded: bool,
    ignore_prior_state: bool = False,
) -> tuple[Decision, str]:
    """Return the decision for one resource and the reason shown to the operator.

    `recorded` means the snapshot says weekend-saver switched this resource off.
    """
    # Something we switched off is always switched back on, even if it was tagged since;
    # otherwise the tag would silently strand it.
    restoring = action is Action.ENABLE and recorded
    if not restoring and _has_skip_tag(status):
        return Decision.SKIP, f"tagged {SKIP_TAG}={SKIP_TAG_VALUE}"
    if action is Action.DISABLE:
        return _decide_disable(status.power_state)
    return _decide_enable(status.power_state, recorded or ignore_prior_state)


def _decide_disable(state: PowerState) -> tuple[Decision, str]:
    if state is PowerState.RUNNING:
        return Decision.ACT, "running; will be disabled"
    if state is PowerState.STOPPED:
        return Decision.SKIP, "already stopped"
    if state is PowerState.TRANSITIONING:
        # Missing one saving is cheaper than interrupting something mid-change
        return Decision.SKIP, "mid-transition; leaving it alone"
    # An unreadable state every week would mean no savings and nobody noticing
    return Decision.ERROR, "power state could not be determined; not disabled"


def _decide_enable(state: PowerState, eligible: bool) -> tuple[Decision, str]:
    if not eligible:
        return Decision.SKIP, "not disabled by weekend-saver"
    if state is PowerState.RUNNING:
        return Decision.SKIP, "already running"
    if state is PowerState.STOPPED:
        return Decision.ACT, "stopped; will be enabled"
    return Decision.ERROR, f"state is {state.value}; cannot confirm it will be running"


def decide_unlisted(state: PowerState) -> tuple[Decision, str]:
    """Decide for a resource the snapshot records but the current list no longer contains."""
    if state is PowerState.RUNNING:
        return Decision.SKIP, "no longer in the resource list; already running"
    return (
        Decision.ERROR,
        f"disabled by weekend-saver but no longer in the resource list, so it was not "
        f"started (state: {state.value}). Start it yourself or add it back to the list.",
    )


def _has_skip_tag(status: ResourceStatus) -> bool:
    # Azure tag names are case-insensitive
    tags = {name.lower(): value for name, value in status.tags.items()}
    return tags.get(SKIP_TAG, "").lower() == SKIP_TAG_VALUE


_UNAPPLIED_OUTCOMES = {
    Decision.ACT: Outcome.PLANNED,
    Decision.SKIP: Outcome.SKIPPED,
    Decision.ERROR: Outcome.FAILED,
}


def unapplied_result(item: PlannedAction) -> ActionResult:
    """Describe a plan item that has not been (or will not be) acted on."""
    return ActionResult(
        resource_id=item.target.resource_id,
        outcome=_UNAPPLIED_OUTCOMES[item.decision],
        detail=item.reason,
        state=item.current_state,
    )


def unapplied_results(plan: ActionPlan) -> tuple[ActionResult, ...]:
    return tuple(unapplied_result(item) for item in plan.items)
