"""An in-memory stand-in for Azure, so the dashboard can be tried without a subscription.

`weekend-saver dashboard --demo` wires these in place of the real sign-in, discovery
and handlers. Nothing here talks to Azure.
"""

import asyncio
from collections.abc import Mapping

from weekend_saver.auth import SignInStatus
from weekend_saver.azure_handlers import OPERATIONS
from weekend_saver.discovery import DiscoveredResource
from weekend_saver.handlers import ResourceHandler
from weekend_saver.models import PowerState, ResourceSpec, ResourceStatus, ResourceTarget

READ_DELAY_SECONDS = 0.15
CHANGE_DELAY_SECONDS = 1.2

_DEV = ("11111111-1111-1111-1111-111111111111", "Demo Dev/Test", "rg-shop-dev")
_QA = ("22222222-2222-2222-2222-222222222222", "Demo QA", "rg-shop-qa")

# (scope, provider/type, name, initial state, tags, default order or None if unmanaged)
_CATALOGUE = (
    (_DEV, "Microsoft.Web/sites", "shop-web-dev", PowerState.RUNNING, {}, 0),
    (_DEV, "Microsoft.Web/sites", "shop-api-dev", PowerState.RUNNING, {}, 0),
    (_DEV, "Microsoft.Compute/virtualMachines", "shop-worker-dev", PowerState.RUNNING, {}, 0),
    (_DEV, "Microsoft.DBforPostgreSQL/flexibleServers", "shop-db-dev", PowerState.RUNNING, {}, 10),
    (_DEV, "Microsoft.Compute/virtualMachines", "build-agent", PowerState.STOPPED, {}, 0),
    (
        _DEV,
        "Microsoft.ContainerService/managedClusters",
        "shop-aks-dev",
        PowerState.RUNNING,
        {},
        None,
    ),
    (
        _DEV,
        "Microsoft.Compute/virtualMachines",
        "jumpbox",
        PowerState.RUNNING,
        {"weekend-saver": "skip"},
        None,
    ),
    (_QA, "Microsoft.Web/sites", "shop-web-qa", PowerState.RUNNING, {}, None),
    (_QA, "Microsoft.DBforMySQL/flexibleServers", "shop-db-qa", PowerState.RUNNING, {}, None),
    (_QA, "Microsoft.Network/applicationGateways", "shop-gw-qa", PowerState.RUNNING, {}, None),
)


def _resource_id(scope: tuple[str, str, str], provider_type: str, name: str) -> str:
    subscription_id, _, group = scope
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{group}/providers/{provider_type}/{name}"
    )


DEMO_RESOURCES: tuple[DiscoveredResource, ...] = tuple(
    DiscoveredResource(
        resource_id=_resource_id(scope, provider_type, name),
        name=name,
        resource_type=provider_type.lower(),
        resource_group=scope[2],
        subscription_id=scope[0],
        subscription_name=scope[1],
        location="westeurope",
        tags=tags,
    )
    for scope, provider_type, name, _, tags, _ in _CATALOGUE
)

# Things a real subscription also contains but weekend-saver cannot stop and start
DEMO_UNSUPPORTED: tuple[DiscoveredResource, ...] = tuple(
    DiscoveredResource(
        resource_id=_resource_id(scope, provider_type, name),
        name=name,
        resource_type=provider_type.lower(),
        resource_group=scope[2],
        subscription_id=scope[0],
        subscription_name=scope[1],
        location="westeurope",
        supported=False,
    )
    for scope, provider_type, name in (
        (_DEV, "Microsoft.Storage/storageAccounts", "shopdevstorage"),
        (_DEV, "Microsoft.KeyVault/vaults", "shop-kv-dev"),
        (_QA, "Microsoft.CognitiveServices/accounts", "shop-openai-qa"),
    )
)

DEMO_SELECTION: tuple[ResourceSpec, ...] = tuple(
    ResourceSpec(_resource_id(scope, provider_type, name), order)
    for scope, provider_type, name, _, _, order in _CATALOGUE
    if order is not None
)


class DemoHandler:
    """A ResourceHandler whose "Azure" is a dict, with delays so progress is visible."""

    def __init__(
        self,
        read_delay_seconds: float = READ_DELAY_SECONDS,
        change_delay_seconds: float = CHANGE_DELAY_SECONDS,
    ) -> None:
        self._read_delay = read_delay_seconds
        self._change_delay = change_delay_seconds
        self._states = {
            _resource_id(scope, provider_type, name).lower(): state
            for scope, provider_type, name, state, _, _ in _CATALOGUE
        }
        self._tags: Mapping[str, Mapping[str, str]] = {
            resource.resource_id.lower(): resource.tags for resource in DEMO_RESOURCES
        }

    async def get_status(self, target: ResourceTarget) -> ResourceStatus:
        await asyncio.sleep(self._read_delay)
        return ResourceStatus(self._states[target.key], self._tags.get(target.key, {}))

    async def disable(self, target: ResourceTarget) -> None:
        await self._change(target, PowerState.STOPPED)

    async def enable(self, target: ResourceTarget) -> None:
        await self._change(target, PowerState.RUNNING)

    async def _change(self, target: ResourceTarget, state: PowerState) -> None:
        self._states[target.key] = PowerState.TRANSITIONING
        await asyncio.sleep(self._change_delay)
        self._states[target.key] = state


def demo_handlers(handler: DemoHandler | None = None) -> dict[str, ResourceHandler]:
    shared = handler or DemoHandler()
    return dict.fromkeys(OPERATIONS, shared)


class DemoAuth:
    def status(self) -> SignInStatus:
        return SignInStatus(True, "demo@example.com", "demo-tenant", "demo")

    def login(self) -> SignInStatus:
        return self.status()


class DemoDiscovery:
    async def list(self) -> tuple[DiscoveredResource, ...]:
        return (*DEMO_RESOURCES, *DEMO_UNSUPPORTED)
