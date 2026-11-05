from types import SimpleNamespace

import pytest

from tests.helpers import SUBSCRIPTION_ID, VM_TYPE, vm_id
from weekend_saver.discovery import RESOURCES_QUERY, AzureDiscovery, discover_resources

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


def test_unsupported_resources_are_listed_and_flagged_not_dropped():
    ai_account = row(
        "my-openai",
        id=f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-test"
        "/providers/Microsoft.CognitiveServices/accounts/my-openai",
        type="Microsoft.CognitiveServices/accounts",
    )
    client = FakeGraphClient([[row("vm-a"), ai_account]], SUBSCRIPTIONS)

    found = {resource.name: resource for resource in discover_resources(client, TYPES)}

    assert found["vm-a"].supported is True
    assert found["my-openai"].supported is False
    assert found["my-openai"].resource_type == "microsoft.cognitiveservices/accounts"


def test_the_resource_query_is_a_constant_with_nothing_spliced_in():
    client = FakeGraphClient([[row("vm-a")]], SUBSCRIPTIONS)

    discover_resources(client, TYPES)

    assert client.requests[0].query == RESOURCES_QUERY
    assert "where" not in RESOURCES_QUERY


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
        credential=object(), supported_types=TYPES, client_factory=lambda _: client
    )

    found = await discovery.list()

    assert [resource.name for resource in found] == ["vm-a"]
