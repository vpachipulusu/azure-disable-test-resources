from datetime import datetime, timedelta

import pytest

from weekend_saver.dashboard.scheduler import (
    Due,
    Schedule,
    Scheduler,
    ScheduleState,
    ScheduleStore,
    WeeklyTime,
    due_actions,
    most_recent,
    next_occurrence,
)
from weekend_saver.errors import ConfigError
from weekend_saver.models import Action

FRIDAY_7PM = WeeklyTime(weekday=4, hour=19, minute=0)
MONDAY_6AM = WeeklyTime(weekday=0, hour=6, minute=0)
SCHEDULE = Schedule(enabled=True, disable_at=FRIDAY_7PM, enable_at=MONDAY_6AM)

# 2026-11-06 is a Friday, 2026-11-09 a Monday
WEDNESDAY = datetime(2026, 11, 4, 12, 0)
FRIDAY_FIRE = datetime(2026, 11, 6, 19, 0)
MONDAY_FIRE = datetime(2026, 11, 9, 6, 0)


def armed(at: datetime = WEDNESDAY, **last_fired: str) -> ScheduleState:
    return ScheduleState(SCHEDULE, armed_at=at.isoformat(), last_fired=last_fired)


def test_most_recent_and_next_occurrence_bracket_now():
    assert most_recent(datetime(2026, 11, 7, 10, 0), FRIDAY_7PM) == FRIDAY_FIRE
    assert most_recent(FRIDAY_FIRE, FRIDAY_7PM) == FRIDAY_FIRE
    assert most_recent(datetime(2026, 11, 6, 18, 59), FRIDAY_7PM) == FRIDAY_FIRE - timedelta(days=7)
    assert next_occurrence(datetime(2026, 11, 7, 10, 0), FRIDAY_7PM) == FRIDAY_FIRE + timedelta(
        days=7
    )
    assert next_occurrence(WEDNESDAY, FRIDAY_7PM) == FRIDAY_FIRE


def test_nothing_is_due_before_the_first_occurrence_after_arming():
    assert due_actions(armed(), WEDNESDAY + timedelta(hours=1)) == []


def test_disable_fires_at_its_time():
    due = due_actions(armed(), FRIDAY_FIRE + timedelta(seconds=40))

    assert due == [Due(Action.DISABLE, FRIDAY_FIRE, missed=False)]


def test_an_action_already_fired_for_this_occurrence_does_not_fire_again():
    state = armed(disable=FRIDAY_FIRE.isoformat())

    assert due_actions(state, FRIDAY_FIRE + timedelta(minutes=2)) == []


def test_arming_the_schedule_does_not_fire_occurrences_from_before():
    saturday = datetime(2026, 11, 7, 9, 0)

    assert due_actions(armed(at=saturday), saturday + timedelta(minutes=1)) == []


def test_a_late_disable_is_reported_as_missed():
    state = armed()

    due = due_actions(state, FRIDAY_FIRE + timedelta(hours=3))

    assert due == [Due(Action.DISABLE, FRIDAY_FIRE, missed=True)]


def test_a_late_enable_is_reported_as_missed():
    state = armed(disable=FRIDAY_FIRE.isoformat())

    due = due_actions(state, MONDAY_FIRE + timedelta(hours=3))

    assert due == [Due(Action.ENABLE, MONDAY_FIRE, missed=True)]


def test_disabled_schedule_is_never_due():
    state = ScheduleState(Schedule(enabled=False), armed_at=WEDNESDAY.isoformat())

    assert due_actions(state, FRIDAY_FIRE) == []


def test_store_round_trips_and_defaults_to_off(tmp_path):
    store = ScheduleStore(tmp_path / "schedule.json")
    assert store.load() == ScheduleState(Schedule(enabled=False))

    state = armed(disable=FRIDAY_FIRE.isoformat())
    store.save(state)

    assert store.load() == state


def test_store_rejects_a_corrupt_file(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text(
        '{"enabled": true, "disable_at": {"weekday": 9, "time": "25:00"}}', encoding="utf-8"
    )

    with pytest.raises(ConfigError):
        ScheduleStore(path).load()


@pytest.mark.parametrize("bad", [(7, 0, 0), (0, 24, 0), (0, 0, 60), (-1, 0, 0)])
def test_weekly_time_validates_its_fields(bad):
    with pytest.raises(ConfigError):
        WeeklyTime(*bad)


class FakeRunManager:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.started: list[tuple[Action, str]] = []

        self.awaiting_approval = False
        self.answers: list[bool] = []
        self.results: dict[str, dict] = {}

    def start(self, request, trigger: str) -> str:
        self.started.append((request, trigger))
        return f"run-{len(self.started)}"

    def answer(self, approved: bool) -> None:
        self.answers.append(approved)
        self.busy = self.awaiting_approval = False

    def result_of(self, run_id: str):
        return self.results.get(run_id)

    def finish(self, run_id: str, *, failed: bool) -> None:
        self.results[run_id] = {"id": run_id, "failed": failed}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def scheduler(tmp_path, now: datetime, state: ScheduleState, *, busy: bool = False):
    store = ScheduleStore(tmp_path / "schedule.json")
    store.save(state)
    runs = FakeRunManager(busy)
    return Scheduler(store, runs, request_for=lambda action: action, clock=lambda: now), runs, store


def watched_scheduler(tmp_path, now: datetime, state: ScheduleState):
    """A scheduler with a movable clock and a record of the problems it reported."""
    store = ScheduleStore(tmp_path / "schedule.json")
    store.save(state)
    runs = FakeRunManager()
    clock = Clock(now)
    problems: list[tuple[Action, str]] = []
    sched = Scheduler(
        store,
        runs,
        request_for=lambda action: action,
        clock=clock,
        on_problem=lambda action, message: problems.append((action, message)),
    )
    return sched, runs, clock, problems


def test_tick_starts_the_due_run_auto_approved_and_remembers_it(tmp_path):
    sched, runs, store = scheduler(tmp_path, FRIDAY_FIRE + timedelta(seconds=10), armed())

    sched.tick()
    sched.tick()

    assert runs.started == [(Action.DISABLE, "schedule")]
    assert store.load().last_fired == {"disable": FRIDAY_FIRE.isoformat()}


def test_tick_waits_while_another_run_is_active(tmp_path):
    sched, runs, store = scheduler(
        tmp_path, FRIDAY_FIRE + timedelta(seconds=10), armed(), busy=True
    )

    sched.tick()

    assert runs.started == []
    assert store.load().last_fired == {}


def test_missed_enable_runs_late_and_says_so(tmp_path):
    state = armed(disable=FRIDAY_FIRE.isoformat())
    sched, runs, _ = scheduler(tmp_path, MONDAY_FIRE + timedelta(hours=3), state)

    sched.tick()

    assert runs.started == [(Action.ENABLE, "missed-schedule")]
    assert "enable" in sched.notices[0]


def test_missed_disable_is_not_run_late_only_reported(tmp_path):
    sched, runs, store = scheduler(tmp_path, FRIDAY_FIRE + timedelta(hours=3), armed())

    sched.tick()
    sched.tick()

    assert runs.started == []
    assert len(sched.notices) == 1
    assert "disable" in sched.notices[0]
    assert store.load().last_fired == {"disable": FRIDAY_FIRE.isoformat()}


def test_missed_enable_is_skipped_once_a_later_disable_time_has_passed(tmp_path):
    # Dashboard was off from Sunday until the following Saturday
    state = armed(disable=FRIDAY_FIRE.isoformat())
    next_saturday = datetime(2026, 11, 14, 10, 0)
    sched, runs, _ = scheduler(tmp_path, next_saturday, state)

    sched.tick()

    assert runs.started == []
    assert len(sched.notices) == 2


def test_a_broken_resource_list_is_reported_once_and_tried_again(tmp_path):
    attempts = []

    def broken_then_fixed(action):
        attempts.append(action)
        if len(attempts) < 3:
            raise ConfigError("resources.yaml not found")
        return action

    store = ScheduleStore(tmp_path / "schedule.json")
    store.save(armed())
    runs = FakeRunManager()
    sched = Scheduler(
        store, runs, request_for=broken_then_fixed, clock=lambda: FRIDAY_FIRE + timedelta(seconds=5)
    )

    sched.tick()
    sched.tick()
    assert runs.started == []
    assert len(sched.notices) == 1
    assert "resources.yaml not found" in sched.notices[0]
    assert store.load().last_fired == {}

    sched.tick()
    assert runs.started == [(Action.DISABLE, "schedule")]


def test_a_forgotten_approval_is_rejected_so_the_schedule_can_run(tmp_path):
    state = armed(disable=FRIDAY_FIRE.isoformat())
    sched, runs, _ = scheduler(tmp_path, MONDAY_FIRE + timedelta(seconds=10), state, busy=True)
    runs.awaiting_approval = True

    sched.tick()
    assert runs.answers == [False]
    assert runs.started == []
    assert "rejected" in sched.notices[0]

    sched.tick()
    assert runs.started == [(Action.ENABLE, "schedule")]


def test_a_failed_scheduled_enable_is_retried_until_it_works(tmp_path):
    state = armed(disable=FRIDAY_FIRE.isoformat())
    sched, runs, clock, _ = watched_scheduler(tmp_path, MONDAY_FIRE + timedelta(seconds=10), state)

    sched.tick()
    runs.finish("run-1", failed=True)
    sched.tick()
    assert len(runs.started) == 1
    assert "trying again" in sched.notices[-1]

    clock.now += timedelta(minutes=2)
    sched.tick()
    assert len(runs.started) == 1

    clock.now += timedelta(minutes=4)
    sched.tick()
    assert runs.started[-1] == (Action.ENABLE, "schedule-retry")

    runs.finish("run-2", failed=False)
    clock.now += timedelta(minutes=30)
    sched.tick()
    assert len(runs.started) == 2


def test_a_scheduled_enable_that_keeps_failing_is_given_up_loudly(tmp_path):
    state = armed(disable=FRIDAY_FIRE.isoformat())
    sched, runs, clock, _ = watched_scheduler(tmp_path, MONDAY_FIRE + timedelta(seconds=10), state)

    sched.tick()
    for _ in range(10):
        runs.finish(f"run-{len(runs.started)}", failed=True)
        sched.tick()
        clock.now += timedelta(minutes=6)
        sched.tick()

    assert len(runs.started) == 6
    assert "will not be retried" in sched.notices[-1]


def test_a_failed_scheduled_disable_is_reported_but_not_retried(tmp_path):
    sched, runs, clock, _ = watched_scheduler(
        tmp_path, FRIDAY_FIRE + timedelta(seconds=10), armed()
    )

    sched.tick()
    runs.finish("run-1", failed=True)
    sched.tick()
    clock.now += timedelta(minutes=6)
    sched.tick()

    assert len(runs.started) == 1
    assert "will not be retried" in sched.notices[-1]


def test_actions_that_did_not_happen_are_reported_as_problems(tmp_path):
    sched, _, _, problems = watched_scheduler(tmp_path, FRIDAY_FIRE + timedelta(hours=3), armed())

    sched.tick()
    sched.tick()

    assert len(problems) == 1
    assert problems[0][0] is Action.DISABLE
    assert "Missed" in problems[0][1]


def test_nothing_starts_when_the_occurrence_cannot_be_remembered(tmp_path):
    class FailingStore(ScheduleStore):
        def update(self, change):
            raise ConfigError("schedule file is locked")

    store = FailingStore(tmp_path / "schedule.json")
    store.save(armed())
    runs = FakeRunManager()
    sched = Scheduler(
        store,
        runs,
        request_for=lambda action: action,
        clock=lambda: FRIDAY_FIRE + timedelta(seconds=5),
    )

    with pytest.raises(ConfigError):
        sched.tick()

    assert runs.started == []


@pytest.mark.parametrize(
    "document",
    [
        '{"enabled": true, "disable_at": {"weekday": 4, "time": "19:00"}, '
        '"enable_at": {"weekday": 0, "time": "06:00"}, "armed_at": "2026-11-04T12:00:00+00:00"}',
        '{"enabled": true, "disable_at": {"weekday": 4, "time": "19:00"}, '
        '"enable_at": {"weekday": 0, "time": "06:00"}, "last_fired": {"disable": "soon"}}',
        "[]",
    ],
)
def test_store_rejects_timestamps_the_scheduler_could_not_compare(tmp_path, document):
    path = tmp_path / "schedule.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError):
        ScheduleStore(path).load()


def test_store_update_applies_a_change_to_the_latest_state(tmp_path):
    store = ScheduleStore(tmp_path / "schedule.json")
    store.save(armed())

    updated = store.update(
        lambda state: ScheduleState(
            state.schedule, state.armed_at, {"enable": "2026-11-09T06:00:00"}
        )
    )

    assert store.load() == updated
    assert updated.last_fired == {"enable": "2026-11-09T06:00:00"}
