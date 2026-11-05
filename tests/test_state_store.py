import json

import pytest

from weekend_saver.errors import StateStoreError
from weekend_saver.state_store import JsonFileStateStore, Snapshot

FRIDAY = "2026-11-06T18:00:00+00:00"
DISABLED = {"/subscriptions/x/vm-1": FRIDAY}
SNAPSHOT = Snapshot(DISABLED, last_disable_run=FRIDAY)


def test_load_returns_none_when_no_snapshot_exists(tmp_path):
    store = JsonFileStateStore(tmp_path / "state.json")

    assert store.load() is None


def test_round_trips_a_snapshot(tmp_path):
    store = JsonFileStateStore(tmp_path / "state.json")

    store.save(SNAPSHOT)

    assert store.load() == SNAPSHOT


def test_save_creates_missing_parent_directories_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"

    JsonFileStateStore(path).save(SNAPSHOT)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled": DISABLED,
        "last_disable_run": FRIDAY,
    }
    assert [entry.name for entry in path.parent.iterdir()] == ["state.json"]


def test_save_replaces_previous_snapshot(tmp_path):
    store = JsonFileStateStore(tmp_path / "state.json")
    store.save(SNAPSHOT)

    store.save(Snapshot())

    assert store.load() == Snapshot()


def test_snapshot_without_a_disable_run_timestamp_loads_as_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "disabled": {}}', encoding="utf-8")

    assert JsonFileStateStore(path).load() == Snapshot({}, None)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "disabled": {}}',
        '{"version": 1}',
        '{"version": 1, "disabled": {"id": 5}}',
        '{"version": 1, "disabled": {}, "last_disable_run": 5}',
    ],
)
def test_load_raises_on_corrupt_or_unexpected_content(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(StateStoreError):
        JsonFileStateStore(path).load()
