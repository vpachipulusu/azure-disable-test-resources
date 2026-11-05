import pytest

from tests.helpers import SUBSCRIPTION_ID, VM_TYPE, vm_id
from weekend_saver.arm import build_targets, parse_resource_id
from weekend_saver.errors import ConfigError
from weekend_saver.models import ResourceSpec

SUPPORTED = frozenset({VM_TYPE})


def test_parses_subscription_group_type_and_name_from_resource_id():
    target = parse_resource_id(vm_id("vm-1", "rg-app"), order=5)

    assert target.subscription_id == SUBSCRIPTION_ID
    assert target.resource_group == "rg-app"
    assert target.resource_type == VM_TYPE
    assert target.name == "vm-1"
    assert target.order == 5


def test_key_ignores_case_and_trailing_slash():
    lower = parse_resource_id(vm_id("vm-1").lower())
    upper = parse_resource_id(vm_id("vm-1").upper() + "/")

    assert lower.key == upper.key


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "not-an-id",
        "/subscriptions/not-a-guid/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg",
        vm_id("vm-1") + "/extensions/child",
        vm_id("vm 1"),
        vm_id(".."),
        vm_id("vm-1", resource_group=".."),
    ],
)
def test_rejects_malformed_or_child_resource_ids(bad_id):
    with pytest.raises(ConfigError):
        parse_resource_id(bad_id)


def test_build_targets_carries_order_from_spec():
    targets = build_targets((ResourceSpec(vm_id("a"), 10), ResourceSpec(vm_id("b"))), SUPPORTED)

    assert [(t.name, t.order) for t in targets] == [("a", 10), ("b", 0)]


def test_build_targets_rejects_unsupported_resource_type():
    storage_id = (
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg"
        "/providers/Microsoft.Storage/storageAccounts/acct"
    )

    with pytest.raises(ConfigError, match=r"microsoft\.storage/storageaccounts"):
        build_targets((ResourceSpec(storage_id),), SUPPORTED)


def test_build_targets_rejects_duplicates_ignoring_case():
    duplicated = (ResourceSpec(vm_id("vm-1")), ResourceSpec(vm_id("VM-1")))

    with pytest.raises(ConfigError, match="more than once"):
        build_targets(duplicated, SUPPORTED)


def test_build_targets_rejects_empty_list():
    with pytest.raises(ConfigError, match="No resources"):
        build_targets((), SUPPORTED)
