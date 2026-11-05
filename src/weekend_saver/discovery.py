"""Find the resources the signed-in account can manage, via Azure Resource Graph."""

import asyncio
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from azure.core.credentials import TokenCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

PAGE_SIZE = 1000
MAX_PAGES = 50

_SUBSCRIPTIONS_QUERY = (
    "ResourceContainers "
    "| where type =~ 'microsoft.resources/subscriptions' "
    "| project subscriptionId, name"
)
_RESOURCE_TYPE = re.compile(r"^[a-z0-9.]+/[a-z0-9]+$")


@dataclass(frozen=True)
class DiscoveredResource:
    resource_id: str
    name: str
    resource_type: str
    resource_group: str
    subscription_id: str
    subscription_name: str
    location: str
    tags: Mapping[str, str] = field(default_factory=dict)


class Discovery(Protocol):
    async def list(self) -> tuple[DiscoveredResource, ...]: ...


def build_resource_query(resource_types: Collection[str]) -> str:
    """Build the Resource Graph query for every resource of the supported types."""
    for resource_type in resource_types:
        # Types are spliced into the query text, so anything unexpected is refused
        if not _RESOURCE_TYPE.match(resource_type):
            raise ValueError(f"Not a lower-case ARM resource type: {resource_type!r}")
    quoted = ", ".join(f"'{resource_type}'" for resource_type in sorted(resource_types))
    return (
        f"Resources | where type in~ ({quoted}) "
        "| project id, name, type, resourceGroup, subscriptionId, location, tags "
        "| order by subscriptionId asc, resourceGroup asc, name asc"
    )


def discover_resources(
    client: Any, resource_types: Collection[str]
) -> tuple[DiscoveredResource, ...]:
    """Query every subscription the credential can see (no subscription list needed)."""
    rows = _query_all(client, build_resource_query(resource_types))
    subscription_names = {
        row["subscriptionId"]: row["name"] for row in _query_all(client, _SUBSCRIPTIONS_QUERY)
    }
    return tuple(_to_resource(row, subscription_names) for row in rows)


def _query_all(client: Any, query: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skip_token: str | None = None
    for _ in range(MAX_PAGES):
        options = QueryRequestOptions(
            skip_token=skip_token, top=PAGE_SIZE, result_format="objectArray"
        )
        response = client.resources(QueryRequest(query=query, options=options))
        rows.extend(response.data)
        skip_token = response.skip_token
        if not skip_token:
            return rows
    raise RuntimeError(f"Azure Resource Graph paging did not finish within {MAX_PAGES} pages")


def _to_resource(
    row: Mapping[str, Any], subscription_names: Mapping[str, str]
) -> DiscoveredResource:
    subscription_id = row["subscriptionId"]
    return DiscoveredResource(
        resource_id=row["id"],
        name=row["name"],
        resource_type=str(row["type"]).lower(),
        resource_group=row["resourceGroup"],
        subscription_id=subscription_id,
        subscription_name=subscription_names.get(subscription_id, subscription_id),
        location=row.get("location") or "",
        tags=dict(row.get("tags") or {}),
    )


class AzureDiscovery:
    def __init__(
        self,
        credential: TokenCredential,
        resource_types: Collection[str],
        *,
        client_factory: Callable[[TokenCredential], Any] = ResourceGraphClient,
    ) -> None:
        self._client = client_factory(credential)
        self._resource_types = resource_types

    async def list(self) -> tuple[DiscoveredResource, ...]:
        return await asyncio.to_thread(discover_resources, self._client, self._resource_types)
