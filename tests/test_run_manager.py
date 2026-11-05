import asyncio
import json
from datetime import datetime

import pytest

from tests.helpers import VM_TYPE, FakeHandler, specs
from weekend_saver.dashboard.history import RunHistory
from weekend_saver.dashboard.run_manager import (
    NoPendingApprovalError,
    RunInProgressError,
    RunManager,
)
from weekend_saver.models import Action, PowerState, RunRequest
from weekend_saver.state_store import JsonFileStateStore
from weekend_saver.workflow import build_workflow

RUNNING = PowerState.RUNNING
NOW = datetime(2026, 11, 6, 19, 0)


def manager_for(handler: FakeHandler, tmp_path) -> tuple[RunManager, RunHistory]:
    store = JsonFileStateStore(tmp_path / "state.json")
    history = RunHistory(tmp_path / "runs.jsonl")

    def workflow_factory():
        return build_workflow(
            {VM_TYPE: handler}, store, verify_attempts=2, verify_interval_seconds=0
        )

    return RunManager(workflow_factory, history, clock=lambda: NOW), history


def drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def next_event(queue: asyncio.Queue, event_type: str) -> dict:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=5)
        if event["type"] == event_type:
            return event


def disable(*names: str, **flags) -> RunRequest:
    return RunRequest(Action.DISABLE, specs(*names), **flags)


async def test_auto_approved_run_narrates_every_step_and_records_history(tmp_path):
    handler = FakeHandler({"app": RUNNING})
    manager, history = manager_for(handler, tmp_path)
    queue = manager.subscribe()

    run_id = manager.start(disable("app", auto_approve=True), trigger="manual")
    record = await manager.wait()

    events = drain(queue)
    assert events[0]["type"] == "run_started"
    assert events[-1] == {"type": "run_finished", "run": record}
    started = [e["step"] for e in events if e["type"] == "step" and e["status"] == "running"]
    assert started == ["load_targets", "resolve_state", "approval", "apply", "verify", "report"]
    assert record["id"] == run_id
    assert (record["status"], record["failed"], record["trigger"]) == ("completed", False, "manual")
    assert record["results"][0]["name"] == "app"
    assert record["results"][0]["outcome"] == "succeeded"
    assert history.recent() == [record]
    assert not manager.busy


async def test_run_pauses_for_approval_and_continues_when_approved(tmp_path):
    handler = FakeHandler({"app": RUNNING})
    manager, _ = manager_for(handler, tmp_path)
    queue = manager.subscribe()

    manager.start(disable("app"), trigger="manual")
    approval = await next_event(queue, "approval_required")

    assert approval["plan"]["actionable"] == 1
    assert approval["plan"]["items"][0]["name"] == "app"
    assert manager.current()["pending_plan"] == approval["plan"]
    assert manager.current()["steps"]["approval"] == "waiting"
    assert handler.calls == []

    manager.answer(True)
    record = await manager.wait()

    assert handler.calls == [("disable", "app")]
    assert record["status"] == "completed"


async def test_rejected_run_changes_nothing(tmp_path):
    handler = FakeHandler({"app": RUNNING})
    manager, _ = manager_for(handler, tmp_path)
    queue = manager.subscribe()
    manager.start(disable("app"), trigger="manual")
    await next_event(queue, "approval_required")

    manager.answer(False)
    record = await manager.wait()

    assert record["status"] == "rejected"
    assert handler.calls == []


async def test_only_one_run_at_a_time(tmp_path):
    manager, _ = manager_for(FakeHandler({"app": RUNNING}), tmp_path)
    queue = manager.subscribe()
    manager.start(disable("app"), trigger="manual")

    with pytest.raises(RunInProgressError):
        manager.start(disable("app"), trigger="schedule")

    await next_event(queue, "approval_required")
    manager.answer(False)
    await manager.wait()


async def test_answering_with_nothing_pending_is_an_error(tmp_path):
    manager, _ = manager_for(FakeHandler({}), tmp_path)

    with pytest.raises(NoPendingApprovalError):
        manager.answer(True)


async def test_crashed_run_is_recorded_and_frees_the_manager(tmp_path):
    def broken_factory():
        raise RuntimeError("secret detail that must not reach the browser")

    history = RunHistory(tmp_path / "runs.jsonl")
    manager = RunManager(broken_factory, history, clock=lambda: NOW)

    manager.start(disable("app", auto_approve=True), trigger="schedule")
    record = await manager.wait()

    assert (record["status"], record["failed"]) == ("crashed", True)
    assert "RuntimeError" in record["error"]
    assert "secret detail" not in record["error"]
    assert not manager.busy


async def test_a_stalled_subscriber_is_told_to_resync_instead_of_blocking_the_run(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("weekend_saver.dashboard.run_manager.SUBSCRIBER_QUEUE_SIZE", 1)
    manager, _ = manager_for(FakeHandler({"app": RUNNING}), tmp_path)
    stalled = manager.subscribe()

    manager.start(disable("app", auto_approve=True), trigger="manual")
    record = await manager.wait()

    assert record["status"] == "completed"
    assert stalled.get_nowait() == {"type": "resync"}


async def test_an_unanswered_approval_times_out_as_a_rejection(tmp_path):
    handler = FakeHandler({"app": RUNNING})
    store = JsonFileStateStore(tmp_path / "state.json")
    manager = RunManager(
        lambda: build_workflow(
            {VM_TYPE: handler}, store, verify_attempts=2, verify_interval_seconds=0
        ),
        RunHistory(tmp_path / "runs.jsonl"),
        clock=lambda: NOW,
        approval_timeout_seconds=0.05,
    )

    manager.start(disable("app"), trigger="manual")
    record = await manager.wait()

    assert record["status"] == "rejected"
    assert "Nobody answered" in record["error"]
    assert handler.calls == []
    assert not manager.busy


async def test_an_answer_for_a_different_run_is_refused(tmp_path):
    manager, _ = manager_for(FakeHandler({"app": RUNNING}), tmp_path)
    queue = manager.subscribe()
    run_id = manager.start(disable("app"), trigger="manual")
    approval = await next_event(queue, "approval_required")

    with pytest.raises(NoPendingApprovalError, match="no longer active"):
        manager.answer(True, run_id="some-earlier-run")

    assert approval["run_id"] == run_id
    assert manager.awaiting_approval
    manager.answer(False, run_id=run_id)
    await manager.wait()


async def test_finished_runs_can_be_looked_up_by_id(tmp_path):
    manager, _ = manager_for(FakeHandler({"app": RUNNING}), tmp_path)

    run_id = manager.start(disable("app", auto_approve=True), trigger="schedule")
    assert manager.result_of(run_id) is None
    record = await manager.wait()

    assert manager.result_of(run_id) == record


async def test_shutdown_rejects_a_waiting_approval_and_records_the_run(tmp_path):
    handler = FakeHandler({"app": RUNNING})
    manager, history = manager_for(handler, tmp_path)
    queue = manager.subscribe()
    manager.start(disable("app"), trigger="manual")
    await next_event(queue, "approval_required")

    await manager.shutdown()

    assert history.recent()[0]["status"] == "rejected"
    assert handler.calls == []
    await manager.shutdown()


def test_history_returns_newest_first_and_skips_unreadable_lines(tmp_path):
    path = tmp_path / "runs.jsonl"
    history = RunHistory(path)
    for number in range(3):
        history.append({"id": str(number)})
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n" + json.dumps(["not", "a", "record"]) + "\n")

    assert [record["id"] for record in history.recent(limit=2)] == ["2", "1"]
    assert RunHistory(tmp_path / "missing.jsonl").recent() == []
