"""The contract every resource type implements to be disabled and enabled."""

from typing import Protocol

from weekend_saver.models import ResourceStatus, ResourceTarget


class ResourceHandler(Protocol):
    async def get_status(self, target: ResourceTarget) -> ResourceStatus: ...

    async def disable(self, target: ResourceTarget) -> None:
        """Stop the resource and return once Azure reports the operation finished."""
        ...

    async def enable(self, target: ResourceTarget) -> None:
        """Start the resource and return once Azure reports the operation finished."""
        ...
