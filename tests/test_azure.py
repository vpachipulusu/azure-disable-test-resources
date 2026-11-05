from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.helpers import SUBSCRIPTION_ID, VM_TYPE, vm_id
from weekend_saver import azure_handlers
from weekend_saver.arm import parse_resource_id
from weekend_saver.azure_handlers import (
    OPERATIONS,
    AzureClients,
    AzureResourceHandler,
    build_azure_handlers,
    wait_for,
)
from weekend_saver.models import PowerState

RUNNING, STOPPED = PowerState.RUNNING, PowerState.STOPPED
TRANSITIONING, UNKNOWN = PowerState.TRANSITIONING, PowerState.UNKNOWN

VMSS_TYPE = "microsoft.compute/virtualmachinescalesets"
AKS_TYPE = "microsoft.containerservice/managedclusters"
WEB_TYPE = "microsoft.web/sites"
POSTGRES_TYPE = "microsoft.dbforpostgresql/flexibleservers"
MYSQL_TYPE = "microsoft.dbformysql/flexibleservers"
GATEWAY_TYPE = "microsoft.network/applicationgateways"


def read(resource_type: str, client) -> PowerState:
    return OPERATIONS[resource_type].read(client, "rg", "name").power_state


def finished_poller() -> MagicMock:
    poller = MagicMock()
    poller.done.return_value = True
    return poller


def vm_client(*codes: str, tags=None) -> MagicMock:
    client = MagicMock()
    client.virtual_machines.get.return_value = SimpleNamespace(
        tags=tags,
        instance_view=SimpleNamespace(statuses=[SimpleNamespace(code=code) for code in codes]),
    )
    return client


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("PowerState/running", RUNNING),
        ("PowerState/deallocated", STOPPED),
        ("PowerState/stopped", STOPPED),
        ("PowerState/deallocating", TRANSITIONING),
        ("PowerState/starting", TRANSITIONING),
        ("PowerState/somethingNew", UNKNOWN),
    ],
)
def test_vm_power_state_comes_from_instance_view(code, expected):
    client = vm_client("ProvisioningState/succeeded", code)

    assert read(VM_TYPE, client) is expected
    client.virtual_machines.get.assert_called_once_with("rg", "name", expand="instanceView")


def test_vm_without_instance_view_is_unknown_and_tags_are_returned():
    client = MagicMock()
    client.virtual_machines.get.return_value = SimpleNamespace(
        tags={"weekend-saver": "skip"}, instance_view=None
    )

    status = OPERATIONS[VM_TYPE].read(client, "rg", "name")

    assert status.power_state is UNKNOWN
    assert status.tags == {"weekend-saver": "skip"}


@pytest.mark.parametrize(
    ("instance_codes", "expected"),
    [
        (["PowerState/running", "PowerState/running"], RUNNING),
        (["PowerState/deallocated", "PowerState/deallocated"], STOPPED),
        (["PowerState/running", "PowerState/deallocated"], RUNNING),
        (["PowerState/running", "PowerState/starting"], TRANSITIONING),
        ([], STOPPED),
    ],
)
def test_scale_set_state_is_combined_from_each_instance(instance_codes, expected):
    client = MagicMock()
    client.virtual_machine_scale_sets.get.return_value = SimpleNamespace(tags=None)
    client.virtual_machine_scale_set_vms.list.return_value = iter(
        [
            SimpleNamespace(
                instance_view=SimpleNamespace(
                    statuses=[
                        SimpleNamespace(code="ProvisioningState/succeeded"),
                        SimpleNamespace(code=code),
                    ]
                )
            )
            for code in instance_codes
        ]
    )

    assert read(VMSS_TYPE, client) is expected
    client.virtual_machine_scale_set_vms.list.assert_called_once_with(
        "rg", "name", expand="instanceView"
    )


@pytest.mark.parametrize("resource_type", sorted(OPERATIONS))
def test_stop_and_start_methods_exist_on_the_real_sdk_client(resource_type):
    # The other tests mock the SDK, so a misspelt method name would pass them
    operations = OPERATIONS[resource_type]
    client = operations.client_type(MagicMock(), SUBSCRIPTION_ID)
    collection = getattr(client, operations.collection)

    assert callable(collection.get)
    assert callable(getattr(collection, operations.stop_method))
    assert callable(getattr(collection, operations.start_method))


def test_scale_set_instance_listing_exists_on_the_real_sdk_client():
    client = OPERATIONS[VMSS_TYPE].client_type(MagicMock(), SUBSCRIPTION_ID)

    assert callable(client.virtual_machine_scale_set_vms.list)


@pytest.mark.parametrize(
    ("provisioning", "power", "expected"),
    [
        ("Succeeded", "Running", RUNNING),
        ("Succeeded", "Stopped", STOPPED),
        ("Stopping", "Running", TRANSITIONING),
        ("Succeeded", None, UNKNOWN),
    ],
)
def test_aks_state_respects_in_flight_provisioning(provisioning, power, expected):
    client = MagicMock()
    client.managed_clusters.get.return_value = SimpleNamespace(
        tags=None,
        provisioning_state=provisioning,
        power_state=SimpleNamespace(code=power) if power else None,
    )

    assert read(AKS_TYPE, client) is expected


@pytest.mark.parametrize(("state", "expected"), [("Running", RUNNING), ("Stopped", STOPPED)])
def test_web_app_state(state, expected):
    client = MagicMock()
    client.web_apps.get.return_value = SimpleNamespace(tags=None, state=state)

    assert read(WEB_TYPE, client) is expected


@pytest.mark.parametrize("resource_type", [POSTGRES_TYPE, MYSQL_TYPE])
@pytest.mark.parametrize(
    ("state", "expected"),
    [("Ready", RUNNING), ("Stopped", STOPPED), ("Updating", TRANSITIONING), ("Dropping", UNKNOWN)],
)
def test_flexible_server_state(resource_type, state, expected):
    client = MagicMock()
    client.servers.get.return_value = SimpleNamespace(tags=None, state=state)

    assert read(resource_type, client) is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("Running", RUNNING), ("Stopped", STOPPED), ("Starting", TRANSITIONING), (None, UNKNOWN)],
)
def test_application_gateway_state(state, expected):
    client = MagicMock()
    client.application_gateways.get.return_value = SimpleNamespace(
        tags=None, operational_state=state
    )

    assert read(GATEWAY_TYPE, client) is expected


@pytest.mark.parametrize(
    ("resource_type", "collection", "stop_method", "start_method"),
    [
        (VM_TYPE, "virtual_machines", "begin_deallocate", "begin_start"),
        (VMSS_TYPE, "virtual_machine_scale_sets", "begin_deallocate", "begin_start"),
        (AKS_TYPE, "managed_clusters", "begin_stop", "begin_start"),
        (POSTGRES_TYPE, "servers", "begin_stop", "begin_start"),
        (MYSQL_TYPE, "servers", "begin_stop", "begin_start"),
        (GATEWAY_TYPE, "application_gateways", "begin_stop", "begin_start"),
    ],
)
def test_long_running_stop_and_start_wait_for_azure(
    resource_type, collection, stop_method, start_method
):
    client = MagicMock()
    operations_group = getattr(client, collection)
    stop_poller, start_poller = finished_poller(), finished_poller()
    getattr(operations_group, stop_method).return_value = stop_poller
    getattr(operations_group, start_method).return_value = start_poller

    OPERATIONS[resource_type].stop(client, "rg", "name")
    OPERATIONS[resource_type].start(client, "rg", "name")

    getattr(operations_group, stop_method).assert_called_once_with("rg", "name")
    getattr(operations_group, start_method).assert_called_once_with("rg", "name")
    stop_poller.wait.assert_called_once()
    start_poller.wait.assert_called_once()


def test_web_app_stop_and_start_are_immediate_calls():
    client = MagicMock()

    OPERATIONS[WEB_TYPE].stop(client, "rg", "name")
    OPERATIONS[WEB_TYPE].start(client, "rg", "name")

    client.web_apps.stop.assert_called_once_with("rg", "name")
    client.web_apps.start.assert_called_once_with("rg", "name")


def test_wait_for_raises_when_azure_does_not_finish_in_time():
    poller = MagicMock()
    poller.done.return_value = False

    with pytest.raises(TimeoutError, match="5s"):
        wait_for(poller, timeout_seconds=5)

    poller.wait.assert_called_once_with(5)


def test_clients_are_created_once_per_type_and_subscription():
    created = []

    def client_type(credential, subscription_id):
        created.append((credential, subscription_id))
        return object()

    clients = AzureClients(credential="cred")

    first = clients.get(client_type, "sub-a")
    again = clients.get(client_type, "sub-a")
    other = clients.get(client_type, "sub-b")

    assert first is again
    assert first is not other
    assert created == [("cred", "sub-a"), ("cred", "sub-b")]


async def test_handler_runs_operations_against_the_targets_subscription_client(monkeypatch):
    client = vm_client("PowerState/running")
    client.virtual_machines.begin_deallocate.return_value = finished_poller()
    client.virtual_machines.begin_start.return_value = finished_poller()
    requested = []

    class FakeClients:
        def get(self, client_type, subscription_id):
            requested.append(subscription_id)
            return client

    handler = AzureResourceHandler(OPERATIONS[VM_TYPE], FakeClients())
    target = parse_resource_id(vm_id("vm-1", "rg-app"))

    status = await handler.get_status(target)
    await handler.disable(target)
    await handler.enable(target)

    assert status.power_state is RUNNING
    assert requested == [SUBSCRIPTION_ID] * 3
    client.virtual_machines.begin_deallocate.assert_called_once_with("rg-app", "vm-1")
    client.virtual_machines.begin_start.assert_called_once_with("rg-app", "vm-1")


def test_build_azure_handlers_covers_every_supported_type():
    handlers = build_azure_handlers(credential=MagicMock())

    assert set(handlers) == set(azure_handlers.OPERATIONS)
    assert set(handlers) == {
        VM_TYPE,
        VMSS_TYPE,
        AKS_TYPE,
        WEB_TYPE,
        POSTGRES_TYPE,
        MYSQL_TYPE,
        GATEWAY_TYPE,
    }
