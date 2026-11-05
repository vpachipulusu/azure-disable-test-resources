"""Load the user's resource list from YAML.

Expected shape:

    resources:
      - /subscriptions/<guid>/resourceGroups/<group>/providers/<namespace>/<type>/<name>
      - id: /subscriptions/<guid>/...
        order: 10
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from weekend_saver.errors import ConfigError
from weekend_saver.files import write_text_atomically
from weekend_saver.models import ResourceSpec


def load_resource_specs(path: Path) -> tuple[ResourceSpec, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Resource file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read resource file {path}: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(document, dict) or not isinstance(document.get("resources"), list):
        raise ConfigError(f"{path} must contain a top-level 'resources' list.")
    return tuple(_to_spec(entry, path) for entry in document["resources"])


def save_resource_specs(path: Path, specs: Iterable[ResourceSpec]) -> None:
    """Write the selection in the shape `load_resource_specs` reads. Comments are not kept."""
    document = {"resources": [{"id": spec.resource_id, "order": spec.order} for spec in specs]}
    header = (
        "# Managed by the weekend-saver dashboard. Lower order = disabled first, enabled last.\n"
    )
    try:
        write_text_atomically(path, header + yaml.safe_dump(document, sort_keys=False))
    except OSError as exc:
        raise ConfigError(f"Could not write resource file {path}: {exc}") from exc


def _to_spec(entry: Any, path: Path) -> ResourceSpec:
    if isinstance(entry, str) and entry.strip():
        return ResourceSpec(entry)
    if isinstance(entry, dict):
        resource_id = entry.get("id")
        order = entry.get("order", 0)
        # bool is an int subclass; `order: true` is a mistake, not order 1
        valid_order = isinstance(order, int) and not isinstance(order, bool)
        if isinstance(resource_id, str) and resource_id.strip() and valid_order:
            return ResourceSpec(resource_id, order)
    raise ConfigError(
        f"Invalid entry in {path}: {entry!r}. Use a resource ID string, "
        "or a mapping with 'id' (string) and optional 'order' (integer)."
    )
