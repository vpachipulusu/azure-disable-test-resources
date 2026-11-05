from types import SimpleNamespace

import pytest

from tests.helpers import SUBSCRIPTION_ID, VM_TYPE, vm_id
from weekend_saver.discovery import AzureDiscovery, build_resource_query, discover_resources

TYPES = frozenset({VM_TYPE, "microsoft.web/sites"})


def row(name: str, **overrides) -> dict:
    return {
        "id": vm_id(name),
        "name": name,
        "type": "Microsoft.Compute/virtualMachines",
        "resourceGroup": "rg-test",
        "subscriptionId": SUBSCRIPTION_ID,
        "location": "westeurope",
        "tags": {"env": "test"},
        **overrides,
    }


class FakeGraphClient:
    """Serves resource pages, then subscription names, like Azure Resource Graph."""

    def __init__(self, resource_pages: list[list[dict]], subscriptions: list[dict]) -> None:
        self._resource_pages = resource_pages
        self._subscriptions = subscriptions
        self.requests: list = []

    def resources(self, request):
        self.requests.append(request)
        if "resourcecontainers" in request.query.lower():
            return SimpleNamespace(data=self._subscriptions, skip_token=None)
        index = int(request.options.skip_token or 0)
        more = index + 1 < len(self._resource_pages)
        return SimpleNamespace(
            data=self._resource_pages[index], skip_token=str(index + 1) if more else None
        )


SUBSCRIPTIONS = [{"subscriptionId": SUBSCRIPTION_ID, "name": "Test Subscription"}]


def test_query_filters_to_the_supported_types():
    query = build_resource_query(TYPES)

    assert "'microsoft.compute/virtualmachines'" in query
    assert "'microsoft.web/sites'" in query
    assert "type in~" in query


def test_query_refuses_a_type_that_could_break_out_of_the_filter():
    with pytest.raises(ValueError, match="resource type"):
        build_resource_query({"x') | project secrets //"})


def test_discovery_follows_paging_and_names_subscriptions():
    client = FakeGraphClient([[row("vm-a")], [row("vm-b")]], SUBSCRIPTIONS)

    found = discover_resources(client, TYPES)

    assert [resource.name for resource in found] == ["vm-a", "vm-b"]
    first = found[0]
    assert first.resource_id == vm_id("vm-a")
    assert first.resource_type == VM_TYPE
    assert first.resource_group == "rg-test"
    assert first.subscription_name == "Test Subscription"
    assert first.tags == {"env": "test"}


def test_discovery_tolerates_missing_tags_and_unknown_subscription_names():
    client = FakeGraphClient([[row("vm-a", tags=None, subscriptionId="other-sub")]], SUBSCRIPTIONS)

    (resource,) = discover_resources(client, TYPES)

    assert resource.tags == {}
    assert resource.subscription_name == "other-sub"


def test_discovery_stops_if_azure_never_finishes_paging():
    class EndlessClient(FakeGraphClient):
        def resources(self, request):
            if "resourcecontainers" in request.query.lower():
                return SimpleNamespace(data=[], skip_token=None)
            return SimpleNamespace(data=[row("vm-a")], skip_token="again")

    with pytest.raises(RuntimeError, match="paging"):
        discover_resources(EndlessClient([], []), TYPES)


async def test_azure_discovery_runs_the_query_off_the_event_loop():
    client = FakeGraphClient([[row("vm-a")]], SUBSCRIPTIONS)
    discovery = AzureDiscovery(
        credential=object(), resource_types=TYPES, client_factory=lambda _: client
    )

    found = await discovery.list()

    assert [resource.name for resource in found] == ["vm-a"]
