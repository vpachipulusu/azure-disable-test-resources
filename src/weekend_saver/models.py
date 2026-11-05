"""Immutable domain and message types passed between workflow executors."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    DISABLE = "disable"
    ENABLE = "enable"


class PowerState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    TRANSITIONING = "transitioning"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    ACT = "act"
    SKIP = "skip"
    ERROR = "error"


class Outcome(StrEnum):
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    DRY_RUN = "dry_run"
    REJECTED = "rejected"
    INVALID = "invalid"


@dataclass(frozen=True)
class ResourceSpec:
    """A resource exactly as the user supplied it, before validation."""

    resource_id: str
    order: int = 0


@dataclass(frozen=True)
class ResourceTarget:
    """A validated top-level ARM resource."""

    resource_id: str
    subscription_id: str
    resource_group: str
    resource_type: str
    name: str
    order: int = 0

    @property
    def key(self) -> str:
        """ARM IDs are case-insensitive, so this is the identity used for lookups."""
        return self.resource_id.lower()


@dataclass(frozen=True)
class ResourceStatus:
    power_state: PowerState
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRequest:
    action: Action
    resources: tuple[ResourceSpec, ...]
    dry_run: bool = False
    auto_approve: bool = False
    ignore_prior_state: bool = False


@dataclass(frozen=True)
class ValidatedRequest:
    request: RunRequest
    targets: tuple[ResourceTarget, ...]


@dataclass(frozen=True)
class PlannedAction:
    target: ResourceTarget
    current_state: PowerState
    decision: Decision
    reason: str


@dataclass(frozen=True)
class ActionPlan:
    request: RunRequest
    items: tuple[PlannedAction, ...]

    @property
    def actionable(self) -> tuple[PlannedAction, ...]:
        return tuple(item for item in self.items if item.decision is Decision.ACT)


@dataclass(frozen=True)
class ApprovalRequest:
    """Sent out of the workflow to ask a human whether the plan may run."""

    plan: ActionPlan


@dataclass(frozen=True)
class ApprovedPlan:
    plan: ActionPlan


@dataclass(frozen=True)
class ActionResult:
    resource_id: str
    outcome: Outcome
    detail: str
    state: PowerState

    @property
    def key(self) -> str:
        return self.resource_id.lower()


@dataclass(frozen=True)
class AppliedPlan:
    action: Action
    results: tuple[ActionResult, ...]
    to_verify: tuple[ResourceTarget, ...]


@dataclass(frozen=True)
class RunReport:
    action: Action
    status: RunStatus
    results: tuple[ActionResult, ...] = ()
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error) or any(result.outcome is Outcome.FAILED for result in self.results)


_CHECKPOINTED = (
    Action,
    PowerState,
    Decision,
    Outcome,
    RunStatus,
    ResourceSpec,
    ResourceTarget,
    RunRequest,
    ValidatedRequest,
    PlannedAction,
    ActionPlan,
    ApprovalRequest,
    ApprovedPlan,
    ActionResult,
    AppliedPlan,
    RunReport,
)

# Checkpoint storage only unpickles types on its allow list; pass this to
# FileCheckpointStorage(allowed_checkpoint_types=...).
CHECKPOINT_TYPES: list[str] = [f"{cls.__module__}:{cls.__qualname__}" for cls in _CHECKPOINTED]
