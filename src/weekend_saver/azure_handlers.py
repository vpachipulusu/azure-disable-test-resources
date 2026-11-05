"""Azure implementations of ResourceHandler.

Each supported resource type is described by an AzureOperations entry: how to read its
power state, and which SDK calls stop and start it. The synchronous management SDK is
used and pushed onto worker threads, which avoids a second (aiohttp) HTTP stack.

Cost notes: VMs and scale sets are *deallocated* (a merely "stopped" VM is still billed).
A stopped App Service still pays for its plan, and a stopped AKS cluster keeps its disks.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from azure.core.credentials import TokenCredential
from azure.core.polling import LROPoller
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.mysqlflexibleservers import MySQLManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.postgresqlflexibleservers import PostgreSQLManagementClient
from azure.mgmt.web import WebSiteManagementClient

from weekend_saver.handlers import ResourceHandler
from weekend_saver.models import PowerState, ResourceStatus, ResourceTarget

LRO_TIMEOUT_SECONDS = 1800.0

Reader = Callable[[Any, str, str], ResourceStatus]


def wait_for(poller: LROPoller[Any], timeout_seconds: float = LRO_TIMEOUT_SECONDS) -> None:
    """Block until the long-running operation finishes; raise if it fails or times out."""
    poller.wait(timeout_seconds)
    if not poller.done():
        raise TimeoutError(f"Azure did not finish the operation within {timeout_seconds:.0f}s")


@dataclass(frozen=True)
class AzureOperations:
    """How one resource type is read, stopped and started through its management client.

    Method names are data rather than code so a test can check them against the real SDK.
    """

    client_type: Callable[[TokenCredential, str], Any]
    collection: str
    read: Reader
    stop_method: str = "begin_stop"
    start_method: str = "begin_start"
    long_running: bool = True

    def stop(self, client: Any, group: str, name: str) -> None:
        self._call(self.stop_method, client, group, name)

    def start(self, client: Any, group: str, name: str) -> None:
        self._call(self.start_method, client, group, name)

    def _call(self, method: str, client: Any, group: str, name: str) -> None:
        result = getattr(getattr(client, self.collection), method)(group, name)
        if self.long_running:
            wait_for(result)


def _lookup(value: str | None, table: Mapping[str, PowerState]) -> PowerState:
    return table.get((value or "").lower(), PowerState.UNKNOWN)


def _tags(resource: Any) -> dict[str, str]:
    return dict(resource.tags or {})


_VM_POWER_STATES = {
    "powerstate/running": PowerState.RUNNING,
    "powerstate/deallocated": PowerState.STOPPED,
    "powerstate/stopped": PowerState.STOPPED,
    "powerstate/starting": PowerState.TRANSITIONING,
    "powerstate/stopping": PowerState.TRANSITIONING,
    "powerstate/deallocating": PowerState.TRANSITIONING,
}
_SERVER_STATES = {
    "ready": PowerState.RUNNING,
    "stopped": PowerState.STOPPED,
    "starting": PowerState.TRANSITIONING,
    "stopping": PowerState.TRANSITIONING,
    "updating": PowerState.TRANSITIONING,
}
_RUN_STATES = {
    "running": PowerState.RUNNING,
    "stopped": PowerState.STOPPED,
    "starting": PowerState.TRANSITIONING,
    "stopping": PowerState.TRANSITIONING,
}
_SETTLED_PROVISIONING_STATES = frozenset({"succeeded", "failed", "canceled"})


def _combined(states: Iterable[PowerState]) -> PowerState:
    """Collapse many instance states into one; anything mid-change wins, then running."""
    present = set(states)
    for candidate in (PowerState.TRANSITIONING, PowerState.RUNNING, PowerState.STOPPED):
        if candidate in present:
            return candidate
    return PowerState.UNKNOWN


def _vm_power_state(vm: Any) -> PowerState:
    statuses = vm.instance_view.statuses if vm.instance_view else None
    # Statuses mix provisioning and power codes; only the power ones are in the table
    return _combined(_lookup(status.code, _VM_POWER_STATES) for status in statuses or ())


def _read_vm(client: Any, group: str, name: str) -> ResourceStatus:
    vm = client.virtual_machines.get(group, name, expand="instanceView")
    return ResourceStatus(_vm_power_state(vm), _tags(vm))


def _read_scale_set(client: Any, group: str, name: str) -> ResourceStatus:
    scale_set = client.virtual_machine_scale_sets.get(group, name)
    # The scale set's own instance view only summarises provisioning states, so power
    # state has to be read from each instance.
    instances = client.virtual_machine_scale_set_vms.list(group, name, expand="instanceView")
    states = [_vm_power_state(instance) for instance in instances]
    # Scaled to zero: nothing is running, so there is nothing to stop or restore
    power_state = _combined(states) if states else PowerState.STOPPED
    return ResourceStatus(power_state, _tags(scale_set))


def _read_aks(client: Any, group: str, name: str) -> ResourceStatus:
    cluster = client.managed_clusters.get(group, name)
    if (cluster.provisioning_state or "").lower() not in _SETTLED_PROVISIONING_STATES:
        return ResourceStatus(PowerState.TRANSITIONING, _tags(cluster))
    code = cluster.power_state.code if cluster.power_state else None
    return ResourceStatus(_lookup(code, _RUN_STATES), _tags(cluster))


def _read_web_app(client: Any, group: str, name: str) -> ResourceStatus:
    site = client.web_apps.get(group, name)
    return ResourceStatus(_lookup(site.state, _RUN_STATES), _tags(site))


def _read_flexible_server(client: Any, group: str, name: str) -> ResourceStatus:
    server = client.servers.get(group, name)
    return ResourceStatus(_lookup(server.state, _SERVER_STATES), _tags(server))


def _read_application_gateway(client: Any, group: str, name: str) -> ResourceStatus:
    gateway = client.application_gateways.get(group, name)
    return ResourceStatus(_lookup(gateway.operational_state, _RUN_STATES), _tags(gateway))


OPERATIONS: Mapping[str, AzureOperations] = {
    "microsoft.compute/virtualmachines": AzureOperations(
        ComputeManagementClient, "virtual_machines", _read_vm, stop_method="begin_deallocate"
    ),
    "microsoft.compute/virtualmachinescalesets": AzureOperations(
        ComputeManagementClient,
        "virtual_machine_scale_sets",
        _read_scale_set,
        stop_method="begin_deallocate",
    ),
    "microsoft.containerservice/managedclusters": AzureOperations(
        ContainerServiceClient, "managed_clusters", _read_aks
    ),
    "microsoft.web/sites": AzureOperations(
        WebSiteManagementClient,
        "web_apps",
        _read_web_app,
        stop_method="stop",
        start_method="start",
        long_running=False,
    ),
    "microsoft.dbforpostgresql/flexibleservers": AzureOperations(
        PostgreSQLManagementClient, "servers", _read_flexible_server
    ),
    "microsoft.dbformysql/flexibleservers": AzureOperations(
        MySQLManagementClient, "servers", _read_flexible_server
    ),
    "microsoft.network/applicationgateways": AzureOperations(
        NetworkManagementClient, "application_gateways", _read_application_gateway
    ),
}


class ClientProvider(Protocol):
    def get(
        self, client_type: Callable[[TokenCredential, str], Any], subscription_id: str
    ) -> Any: ...


class AzureClients:
    """Creates one management client per (client type, subscription) and reuses it."""

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential
        self._clients: dict[tuple[Callable[..., Any], str], Any] = {}

    def get(self, client_type: Callable[[TokenCredential, str], Any], subscription_id: str) -> Any:
        key = (client_type, subscription_id)
        if key not in self._clients:
            self._clients[key] = client_type(self._credential, subscription_id)
        return self._clients[key]


class AzureResourceHandler:
    def __init__(self, operations: AzureOperations, clients: ClientProvider) -> None:
        self._operations = operations
        self._clients = clients

    async def get_status(self, target: ResourceTarget) -> ResourceStatus:
        return await self._in_thread(self._operations.read, target)

    async def disable(self, target: ResourceTarget) -> None:
        await self._in_thread(self._operations.stop, target)

    async def enable(self, target: ResourceTarget) -> None:
        await self._in_thread(self._operations.start, target)

    async def _in_thread(
        self, operation: Callable[[Any, str, str], Any], target: ResourceTarget
    ) -> Any:
        # Resolved on the event-loop thread so the client cache is never touched concurrently
        client = self._clients.get(self._operations.client_type, target.subscription_id)
        return await asyncio.to_thread(operation, client, target.resource_group, target.name)


def build_azure_handlers(credential: TokenCredential) -> dict[str, ResourceHandler]:
    clients = AzureClients(credential)
    return {
        resource_type: AzureResourceHandler(operations, clients)
        for resource_type, operations in OPERATIONS.items()
    }
