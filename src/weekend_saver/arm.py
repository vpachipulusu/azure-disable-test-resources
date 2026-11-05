"""Parse and validate the ARM resource IDs supplied by the user."""

import re
from collections.abc import Collection, Iterable

from weekend_saver.errors import ConfigError
from weekend_saver.models import ResourceSpec, ResourceTarget

# Top-level resources only: /subscriptions/{guid}/resourceGroups/{rg}/providers/{ns}/{type}/{name}
_RESOURCE_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    r"/resourceGroups/(?!\.+/)(?P<group>[-\w.()]{1,90})"
    r"/providers/(?P<namespace>[a-z0-9.]+)/(?P<type>[a-z0-9]+)/(?!\.+$)(?P<name>[-\w.()]+)$",
    re.IGNORECASE,
)


def parse_resource_id(resource_id: str, order: int = 0) -> ResourceTarget:
    normalised = resource_id.strip().rstrip("/")
    match = _RESOURCE_ID.match(normalised)
    if match is None:
        raise ConfigError(
            f"Not a top-level ARM resource ID: {resource_id!r}. Expected "
            "/subscriptions/<guid>/resourceGroups/<group>/providers/<namespace>/<type>/<name>"
        )
    return ResourceTarget(
        resource_id=normalised,
        subscription_id=match["subscription"].lower(),
        resource_group=match["group"],
        resource_type=f"{match['namespace']}/{match['type']}".lower(),
        name=match["name"],
        order=order,
    )


def build_targets(
    specs: Iterable[ResourceSpec], supported_types: Collection[str]
) -> tuple[ResourceTarget, ...]:
    """Validate every spec; raise ConfigError naming the first problem found."""
    targets = tuple(parse_resource_id(spec.resource_id, spec.order) for spec in specs)
    if not targets:
        raise ConfigError("No resources were supplied.")

    seen: set[str] = set()
    for target in targets:
        if target.resource_type not in supported_types:
            raise ConfigError(
                f"Unsupported resource type {target.resource_type!r} for {target.resource_id}. "
                f"Supported: {', '.join(sorted(supported_types))}"
            )
        if target.key in seen:
            raise ConfigError(f"Resource listed more than once: {target.resource_id}")
        seen.add(target.key)
    return targets
