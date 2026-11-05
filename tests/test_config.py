import pytest

from tests.helpers import vm_id
from weekend_saver.config import load_resource_specs, save_resource_specs
from weekend_saver.errors import ConfigError
from weekend_saver.models import ResourceSpec


def write(tmp_path, content: str):
    path = tmp_path / "resources.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_bare_string_and_mapping_entries(tmp_path):
    path = write(
        tmp_path,
        f"resources:\n  - {vm_id('app')}\n  - id: {vm_id('db')}\n    order: 10\n",
    )

    loaded = load_resource_specs(path)

    assert loaded == (ResourceSpec(vm_id("app"), 0), ResourceSpec(vm_id("db"), 10))


def test_saved_selection_loads_back_unchanged(tmp_path):
    path = tmp_path / "nested" / "resources.yaml"
    selection = (ResourceSpec(vm_id("app"), 0), ResourceSpec(vm_id("db"), 10))

    save_resource_specs(path, selection)

    assert load_resource_specs(path) == selection
    assert [entry.name for entry in path.parent.iterdir()] == ["resources.yaml"]


def test_raises_when_file_is_missing(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_resource_specs(tmp_path / "absent.yaml")


def test_raises_on_invalid_yaml(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_resource_specs(write(tmp_path, "resources: [unclosed"))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        "resources: {}",
        "resources:\n  - 42\n",
        "resources:\n  - order: 1\n",
        "resources:\n  - id: some-id\n    order: soon\n",
        "resources:\n  - id: some-id\n    order: true\n",
        "resources:\n  - id: ''\n",
    ],
)
def test_rejects_unexpected_shapes(tmp_path, content):
    with pytest.raises(ConfigError):
        load_resource_specs(write(tmp_path, content))
