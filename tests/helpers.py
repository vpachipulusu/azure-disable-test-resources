"""Shared test fixtures: synthetic ARM IDs and an in-memory fake of the Azure control plane."""

from collections.abc import Iterable, Mapping

from weekend_saver.models import PowerState, ResourceSpec, ResourceStatus, ResourceTarget

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000001"
VM_TYPE = "microsoft.compute/virtualmachines"


def vm_id(name: str, resource_group: str = "rg-test") -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{name}"
    )


def specs(*names: str, orders: Mapping[str, int] | None = None) -> tuple[ResourceSpec, ...]:
    orders = orders or {}
    return tuple(ResourceSpec(vm_id(name), orders.get(name, 0)) for name in names)


class FakeHandler:
    """Stands in for a ResourceHandler; `states` plays the role of Azure."""

    def __init__(
        self,
        states: Mapping[str, PowerState],
        *,
        tags: Mapping[str, Mapping[str, str]] | None = None,
        fail_action_on: Iterable[str] = (),
        fail_status_on: Iterable[str] = (),
        stuck: Iterable[str] = (),
    ) -> None:
        self.states = dict(states)
        self.calls: list[tuple[str, str]] = []
        self._tags = dict(tags or {})
        self._fail_action_on = frozenset(fail_action_on)
        self._fail_status_on = frozenset(fail_status_on)
        self._stuck = frozenset(stuck)

    async def get_status(self, target: ResourceTarget) -> ResourceStatus:
        if target.name in self._fail_status_on:
            raise RuntimeError("status lookup exploded")
        return ResourceStatus(self.states[target.name], self._tags.get(target.name, {}))

    async def disable(self, target: ResourceTarget) -> None:
        self._act("disable", target, PowerState.STOPPED)

    async def enable(self, target: ResourceTarget) -> None:
        self._act("enable", target, PowerState.RUNNING)

    def _act(self, action: str, target: ResourceTarget, new_state: PowerState) -> None:
        if target.name in self._fail_action_on:
            raise RuntimeError("azure said no")
        self.calls.append((action, target.name))
        if target.name not in self._stuck:
            self.states[target.name] = new_state
