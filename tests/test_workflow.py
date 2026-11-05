from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import FileCheckpointStorage

from tests.helpers import VM_TYPE, FakeHandler, specs, vm_id
from weekend_saver.errors import ConfigError, StateStoreError
from weekend_saver.models import (
    CHECKPOINT_TYPES,
    Action,
    ApprovalRequest,
    Outcome,
    PowerState,
    ResourceSpec,
    RunReport,
    RunRequest,
    RunStatus,
)
from weekend_saver.runner import resume_with_decision, run_to_completion
from weekend_saver.state_store import JsonFileStateStore, Snapshot
from weekend_saver.workflow import WORKFLOW_NAME, build_workflow

RUNNING, STOPPED = PowerState.RUNNING, PowerState.STOPPED
FRIDAY = "2026-11-06T18:00:00+00:00"
MONDAY = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


async def approve(_: ApprovalRequest) -> bool:
    return True


async def reject(_: ApprovalRequest) -> bool:
    return False


async def must_not_ask(_: ApprovalRequest) -> bool:
    raise AssertionError("approval was requested but should not have been")


@pytest.fixture
def store(tmp_path) -> JsonFileStateStore:
    return JsonFileStateStore(tmp_path / "state.json")


def workflow_for(handler: FakeHandler, store, **options):
    return build_workflow(
        {VM_TYPE: handler},
        store,
        verify_attempts=2,
        verify_interval_seconds=0,
        clock=lambda: MONDAY,
        **options,
    )


def key(name: str) -> str:
    return vm_id(name).lower()


def seed(store, *names: str, last_disable_run: str = FRIDAY) -> None:
    store.save(Snapshot({key(name): FRIDAY for name in names}, last_disable_run))


def outcomes(report: RunReport) -> dict[str, Outcome]:
    return {result.resource_id.rsplit("/", 1)[-1]: result.outcome for result in report.results}


def disable(*names: str, **flags) -> RunRequest:
    return RunRequest(Action.DISABLE, specs(*names), **flags)


def enable(*names: str, **flags) -> RunRequest:
    return RunRequest(Action.ENABLE, specs(*names), **flags)


async def test_disable_stops_running_resources_and_records_only_those(store):
    handler = FakeHandler({"app": RUNNING, "idle": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), disable("app", "idle", auto_approve=True), must_not_ask
    )

    assert report.status is RunStatus.COMPLETED
    assert not report.failed
    assert handler.calls == [("disable", "app")]
    assert outcomes(report) == {"app": Outcome.SUCCEEDED, "idle": Outcome.SKIPPED}
    assert store.load() == Snapshot({key("app"): MONDAY.isoformat()}, MONDAY.isoformat())


async def test_disable_with_nothing_to_stop_still_stamps_the_snapshot(store):
    handler = FakeHandler({"idle": STOPPED})

    await run_to_completion(
        workflow_for(handler, store), disable("idle", auto_approve=True), must_not_ask
    )

    assert store.load() == Snapshot({}, MONDAY.isoformat())


async def test_enable_restores_only_what_was_disabled_and_clears_the_snapshot(store):
    seed(store, "app")
    handler = FakeHandler({"app": STOPPED, "idle": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), enable("app", "idle", auto_approve=True), must_not_ask
    )

    assert handler.calls == [("enable", "app")]
    assert outcomes(report) == {"app": Outcome.SUCCEEDED, "idle": Outcome.SKIPPED}
    assert store.load() == Snapshot({}, FRIDAY)


async def test_enable_fails_loudly_when_no_snapshot_exists(store):
    handler = FakeHandler({"app": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert report.status is RunStatus.INVALID
    assert report.failed
    assert "--ignore-prior-state" in report.error
    assert handler.calls == []


async def test_enable_refuses_a_snapshot_whose_last_disable_run_is_too_old(store):
    # What a lost Friday snapshot looks like: last week's file, emptied by last Monday's enable
    seed(store, last_disable_run="2026-10-30T18:00:00+00:00")
    handler = FakeHandler({"app": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert report.status is RunStatus.INVALID
    assert "9 days ago" in report.error
    assert "--ignore-prior-state" in report.error
    assert handler.calls == []


async def test_enable_with_ignore_prior_state_starts_every_stopped_resource(store):
    handler = FakeHandler({"app": STOPPED, "db": STOPPED, "up": RUNNING})
    request = enable("app", "db", "up", auto_approve=True, ignore_prior_state=True)

    report = await run_to_completion(workflow_for(handler, store), request, must_not_ask)

    assert sorted(handler.calls) == [("enable", "app"), ("enable", "db")]
    assert not report.failed


async def test_ignore_prior_state_still_honours_the_skip_tag(store):
    handler = FakeHandler({"keep": STOPPED}, tags={"keep": {"weekend-saver": "skip"}})
    request = enable("keep", auto_approve=True, ignore_prior_state=True)

    report = await run_to_completion(workflow_for(handler, store), request, must_not_ask)

    assert outcomes(report) == {"keep": Outcome.SKIPPED}
    assert handler.calls == []


async def test_enable_restores_a_recorded_resource_even_if_tagged_skip_since(store):
    seed(store, "app")
    handler = FakeHandler({"app": STOPPED}, tags={"app": {"weekend-saver": "skip"}})

    await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert handler.calls == [("enable", "app")]


async def test_enable_forgets_resources_someone_already_started(store):
    seed(store, "app")
    handler = FakeHandler({"app": RUNNING})

    await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert handler.calls == []
    assert store.load().disabled == {}


async def test_enable_flags_a_disabled_resource_that_was_dropped_from_the_list(store):
    seed(store, "app", "dropped")
    handler = FakeHandler({"app": STOPPED, "dropped": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"app": Outcome.SUCCEEDED, "dropped": Outcome.FAILED}
    assert handler.calls == [("enable", "app")]
    assert set(store.load().disabled) == {key("dropped")}


async def test_enable_forgets_a_dropped_resource_that_is_running_again(store):
    seed(store, "dropped")
    handler = FakeHandler({"app": RUNNING, "dropped": RUNNING})

    report = await run_to_completion(
        workflow_for(handler, store), enable("app", auto_approve=True), must_not_ask
    )

    assert not report.failed
    assert store.load().disabled == {}


async def test_low_order_is_disabled_first_and_enabled_last(store):
    ordered = specs("db", "app", orders={"db": 10, "app": 0})
    handler = FakeHandler({"app": RUNNING, "db": RUNNING})

    await run_to_completion(
        workflow_for(handler, store),
        RunRequest(Action.DISABLE, ordered, auto_approve=True),
        must_not_ask,
    )
    await run_to_completion(
        workflow_for(handler, store),
        RunRequest(Action.ENABLE, ordered, auto_approve=True),
        must_not_ask,
    )

    assert handler.calls == [
        ("disable", "app"),
        ("disable", "db"),
        ("enable", "db"),
        ("enable", "app"),
    ]


async def test_dry_run_reports_the_plan_without_touching_anything(store):
    handler = FakeHandler({"app": RUNNING, "idle": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, store), disable("app", "idle", dry_run=True), must_not_ask
    )

    assert report.status is RunStatus.DRY_RUN
    assert outcomes(report) == {"app": Outcome.PLANNED, "idle": Outcome.SKIPPED}
    assert handler.calls == []
    assert store.load() is None


async def test_approval_shows_the_plan_and_proceeds_when_approved(store):
    handler = FakeHandler({"app": RUNNING})
    seen: list[ApprovalRequest] = []

    async def approve_and_capture(request: ApprovalRequest) -> bool:
        seen.append(request)
        return True

    report = await run_to_completion(
        workflow_for(handler, store), disable("app"), approve_and_capture
    )

    assert len(seen) == 1
    assert [item.target.name for item in seen[0].plan.actionable] == ["app"]
    assert handler.calls == [("disable", "app")]
    assert report.status is RunStatus.COMPLETED


async def test_approval_applies_a_fresh_plan_not_the_one_that_was_shown(store):
    handler = FakeHandler({"app": RUNNING, "db": RUNNING})

    async def approve_after_someone_stops_db(_: ApprovalRequest) -> bool:
        handler.states["db"] = STOPPED
        return True

    report = await run_to_completion(
        workflow_for(handler, store), disable("app", "db"), approve_after_someone_stops_db
    )

    assert handler.calls == [("disable", "app")]
    assert outcomes(report) == {"app": Outcome.SUCCEEDED, "db": Outcome.SKIPPED}
    assert set(store.load().disabled) == {key("app")}


async def test_rejected_plan_changes_nothing(store):
    handler = FakeHandler({"app": RUNNING})

    report = await run_to_completion(workflow_for(handler, store), disable("app"), reject)

    assert report.status is RunStatus.REJECTED
    assert not report.failed
    assert handler.calls == []
    assert store.load() is None


async def test_approval_is_not_requested_when_there_is_nothing_to_do(store):
    handler = FakeHandler({"idle": STOPPED})

    report = await run_to_completion(workflow_for(handler, store), disable("idle"), must_not_ask)

    assert report.status is RunStatus.COMPLETED
    assert outcomes(report) == {"idle": Outcome.SKIPPED}


async def test_one_failing_resource_does_not_stop_the_others(store):
    handler = FakeHandler({"bad": RUNNING, "good": RUNNING}, fail_action_on={"bad"})

    report = await run_to_completion(
        workflow_for(handler, store), disable("bad", "good", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"bad": Outcome.FAILED, "good": Outcome.SUCCEEDED}
    assert report.failed


async def test_failure_detail_names_the_error_without_echoing_its_message(store):
    handler = FakeHandler({"bad": RUNNING}, fail_action_on={"bad"})

    report = await run_to_completion(
        workflow_for(handler, store), disable("bad", auto_approve=True), must_not_ask
    )

    assert "RuntimeError" in report.results[0].detail
    assert "azure said no" not in report.results[0].detail


async def test_resource_that_never_reaches_target_state_is_reported_failed(store):
    handler = FakeHandler({"app": RUNNING}, stuck={"app"})

    report = await run_to_completion(
        workflow_for(handler, store), disable("app", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"app": Outcome.FAILED}
    assert "running" in report.results[0].detail


async def test_verification_retries_after_a_transient_read_failure(store):
    class FlakyOnSecondRead(FakeHandler):
        reads = 0

        async def get_status(self, target):
            self.reads += 1
            if self.reads == 2:
                raise RuntimeError("throttled")
            return await super().get_status(target)

    handler = FlakyOnSecondRead({"app": RUNNING})

    report = await run_to_completion(
        workflow_for(handler, store), disable("app", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"app": Outcome.SUCCEEDED}
    assert handler.reads == 3


async def test_unreadable_resource_is_reported_failed_and_left_alone(store):
    handler = FakeHandler({"blind": RUNNING, "app": RUNNING}, fail_status_on={"blind"})

    report = await run_to_completion(
        workflow_for(handler, store), disable("blind", "app", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"blind": Outcome.FAILED, "app": Outcome.SUCCEEDED}
    assert handler.calls == [("disable", "app")]


async def test_disable_reports_a_resource_in_an_unknown_state_as_failed(store):
    handler = FakeHandler({"odd": PowerState.UNKNOWN})

    report = await run_to_completion(
        workflow_for(handler, store), disable("odd", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"odd": Outcome.FAILED}
    assert handler.calls == []


async def test_disable_touches_nothing_when_the_snapshot_cannot_be_written(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    unwritable = JsonFileStateStore(blocker / "state.json")
    handler = FakeHandler({"app": RUNNING})

    with pytest.raises(Exception, match="state file"):
        await run_to_completion(
            workflow_for(handler, unwritable), disable("app", auto_approve=True), must_not_ask
        )

    assert handler.calls == []


async def test_report_survives_a_state_file_failure_after_enabling(store):
    seed(store, "app")

    class ReadOnlyStore:
        def load(self) -> Snapshot | None:
            return store.load()

        def save(self, snapshot: Snapshot) -> None:
            raise StateStoreError("disk full")

    handler = FakeHandler({"app": STOPPED})

    report = await run_to_completion(
        workflow_for(handler, ReadOnlyStore()), enable("app", auto_approve=True), must_not_ask
    )

    assert handler.calls == [("enable", "app")]
    assert outcomes(report) == {"app": Outcome.SUCCEEDED}
    assert report.failed
    assert "disk full" in report.error


async def test_skip_tag_protects_a_running_resource(store):
    handler = FakeHandler({"keep": RUNNING}, tags={"keep": {"weekend-saver": "skip"}})

    report = await run_to_completion(
        workflow_for(handler, store), disable("keep", auto_approve=True), must_not_ask
    )

    assert outcomes(report) == {"keep": Outcome.SKIPPED}
    assert handler.calls == []


async def test_invalid_resource_id_ends_the_run_before_any_azure_call(store):
    handler = FakeHandler({})
    request = RunRequest(Action.DISABLE, (ResourceSpec("not-an-arm-id"),), auto_approve=True)

    report = await run_to_completion(workflow_for(handler, store), request, must_not_ask)

    assert report.status is RunStatus.INVALID
    assert "not-an-arm-id" in report.error


async def test_notifier_receives_the_final_report(store):
    received: list[RunReport] = []

    class RecordingNotifier:
        async def notify(self, report: RunReport) -> None:
            received.append(report)

    handler = FakeHandler({"app": RUNNING})
    workflow = workflow_for(handler, store, notifier=RecordingNotifier())

    report = await run_to_completion(workflow, disable("app", auto_approve=True), must_not_ask)

    assert received == [report]


async def test_failing_notifier_does_not_lose_the_report(store):
    class BrokenNotifier:
        async def notify(self, report: RunReport) -> None:
            raise RuntimeError("webhook down")

    handler = FakeHandler({"app": RUNNING})
    workflow = workflow_for(handler, store, notifier=BrokenNotifier())

    report = await run_to_completion(workflow, disable("app", auto_approve=True), must_not_ask)

    assert report.status is RunStatus.COMPLETED


def checkpoint_storage(tmp_path) -> FileCheckpointStorage:
    return FileCheckpointStorage(
        tmp_path / "checkpoints", allowed_checkpoint_types=CHECKPOINT_TYPES
    )


async def test_pending_approval_survives_a_restart_via_checkpoint(store, tmp_path):
    checkpoints = checkpoint_storage(tmp_path)
    handler = FakeHandler({"app": RUNNING})
    paused = await workflow_for(handler, store, checkpoint_storage=checkpoints).run(disable("app"))
    assert len(paused.get_request_info_events()) == 1

    restarted = workflow_for(handler, store, checkpoint_storage=checkpoints)
    report = await resume_with_decision(restarted, checkpoints, WORKFLOW_NAME, approved=True)

    assert report.status is RunStatus.COMPLETED
    assert handler.calls == [("disable", "app")]


async def test_resume_refuses_an_approval_that_has_been_pending_too_long(store, tmp_path):
    checkpoints = checkpoint_storage(tmp_path)
    handler = FakeHandler({"app": RUNNING})
    await workflow_for(handler, store, checkpoint_storage=checkpoints).run(disable("app"))
    restarted = workflow_for(handler, store, checkpoint_storage=checkpoints)

    with pytest.raises(ConfigError, match="too old"):
        await resume_with_decision(
            restarted,
            checkpoints,
            WORKFLOW_NAME,
            approved=True,
            now=datetime.now(UTC) + timedelta(days=2),
        )

    assert handler.calls == []


async def test_resume_with_nothing_pending_is_an_error(store, tmp_path):
    checkpoints = checkpoint_storage(tmp_path)
    workflow = workflow_for(FakeHandler({}), store, checkpoint_storage=checkpoints)

    with pytest.raises(ConfigError, match="No run is waiting"):
        await resume_with_decision(workflow, checkpoints, WORKFLOW_NAME, approved=True)
