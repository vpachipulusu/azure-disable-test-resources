"""Fire the weekly disable and enable runs while the dashboard is open.

Times are in the PC's local time. schedule.json:
    {"enabled": bool,
     "disable_at": {"weekday": 0-6 (Monday=0), "time": "HH:MM"},
     "enable_at":  {"weekday": 0-6, "time": "HH:MM"},
     "armed_at": "<local ISO timestamp>" | null,
     "last_fired": {"disable": "<occurrence ISO>", "enable": "<occurrence ISO>"}}
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from weekend_saver.errors import ConfigError, WeekendSaverError
from weekend_saver.executors import describe_error
from weekend_saver.files import write_text_atomically
from weekend_saver.models import Action, RunRequest

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
# A run starting within this window of its time counts as on time; later is "missed"
ON_TIME_WINDOW = timedelta(minutes=10)
# A failed scheduled enable is tried again: the usual cause is no network right after wake-up
RETRY_INTERVAL = timedelta(minutes=5)
MAX_ENABLE_ATTEMPTS = 6
MAX_NOTICES = 20
WEEK = timedelta(days=7)


@dataclass(frozen=True)
class WeeklyTime:
    weekday: int
    hour: int
    minute: int

    def __post_init__(self) -> None:
        if not (0 <= self.weekday <= 6 and 0 <= self.hour <= 23 and 0 <= self.minute <= 59):
            raise ConfigError(f"Not a valid weekly time: {self}")


@dataclass(frozen=True)
class Schedule:
    enabled: bool = False
    disable_at: WeeklyTime = WeeklyTime(weekday=4, hour=19, minute=0)
    enable_at: WeeklyTime = WeeklyTime(weekday=0, hour=6, minute=0)

    def time_for(self, action: Action) -> WeeklyTime:
        return self.disable_at if action is Action.DISABLE else self.enable_at


@dataclass(frozen=True)
class ScheduleState:
    schedule: Schedule = Schedule()
    # Occurrences before this moment are ignored, so switching the schedule on mid-week
    # does not immediately "catch up" on last Friday.
    armed_at: str | None = None
    last_fired: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Due:
    action: Action
    occurrence: datetime
    missed: bool


def most_recent(now: datetime, at: WeeklyTime) -> datetime:
    """The latest occurrence of `at` that is not after `now`."""
    candidate = (now - timedelta(days=(now.weekday() - at.weekday) % 7)).replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0
    )
    return candidate if candidate <= now else candidate - WEEK


def next_occurrence(now: datetime, at: WeeklyTime) -> datetime:
    return most_recent(now, at) + WEEK


def due_actions(state: ScheduleState, now: datetime) -> list[Due]:
    """Occurrences that have come up since arming and have not been handled yet."""
    if not state.schedule.enabled or state.armed_at is None:
        return []
    armed_at = datetime.fromisoformat(state.armed_at)
    due = []
    for action in (Action.DISABLE, Action.ENABLE):
        occurrence = most_recent(now, state.schedule.time_for(action))
        handled = state.last_fired.get(action.value)
        if occurrence <= armed_at or (handled and datetime.fromisoformat(handled) >= occurrence):
            continue
        due.append(Due(action, occurrence, missed=now - occurrence > ON_TIME_WINDOW))
    return sorted(due, key=lambda item: item.occurrence)


class ScheduleStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        # The scheduler (event loop) and the save endpoint (worker thread) both rewrite the file
        self._lock = threading.RLock()

    def load(self) -> ScheduleState:
        with self._lock:
            return self._read()

    def save(self, state: ScheduleState) -> None:
        with self._lock:
            self._write(state)

    def update(self, change: Callable[[ScheduleState], ScheduleState]) -> ScheduleState:
        """Apply `change` to the stored state as one step, so concurrent writers cannot clash."""
        with self._lock:
            current = self._read()
            updated = change(current)
            if updated != current:
                self._write(updated)
            return updated

    def _read(self) -> ScheduleState:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ScheduleState()
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read schedule file {self._path}: {exc}") from exc
        try:
            return ScheduleState(
                Schedule(
                    enabled=bool(document["enabled"]),
                    disable_at=_weekly_time(document["disable_at"]),
                    enable_at=_weekly_time(document["enable_at"]),
                ),
                armed_at=_local_timestamp(document.get("armed_at")),
                last_fired={
                    str(action): _local_timestamp(fired)
                    for action, fired in (document.get("last_fired") or {}).items()
                },
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Schedule file {self._path} is malformed: {exc}") from exc

    def _write(self, state: ScheduleState) -> None:
        document = {
            "enabled": state.schedule.enabled,
            "disable_at": _weekly_time_json(state.schedule.disable_at),
            "enable_at": _weekly_time_json(state.schedule.enable_at),
            "armed_at": state.armed_at,
            "last_fired": dict(state.last_fired),
        }
        try:
            write_text_atomically(self._path, json.dumps(document, indent=2))
        except OSError as exc:
            raise ConfigError(f"Could not write schedule file {self._path}: {exc}") from exc


def _weekly_time(document: Mapping[str, Any]) -> WeeklyTime:
    hour, minute = str(document["time"]).split(":")
    return WeeklyTime(int(document["weekday"]), int(hour), int(minute))


def _weekly_time_json(at: WeeklyTime) -> dict[str, Any]:
    return {"weekday": at.weekday, "time": f"{at.hour:02d}:{at.minute:02d}"}


def _local_timestamp(value: Any) -> str | None:
    """Accept only what the scheduler itself writes: a naive local ISO timestamp."""
    if value is None:
        return None
    if datetime.fromisoformat(value).tzinfo is not None:
        raise ValueError(f"timestamp {value!r} must not carry a time zone")
    return value


class Runs(Protocol):
    """The part of the run manager the scheduler needs."""

    @property
    def busy(self) -> bool: ...

    @property
    def awaiting_approval(self) -> bool: ...

    def start(self, request: RunRequest, trigger: str) -> str: ...

    def answer(self, approved: bool) -> None: ...

    def result_of(self, run_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class _Watched:
    """A scheduled run whose outcome still matters to the scheduler."""

    run_id: str
    action: Action
    occurrence: datetime
    attempts: int
    retry_at: datetime | None = None


class Scheduler:
    def __init__(
        self,
        store: ScheduleStore,
        runs: Runs,
        request_for: Callable[[Action], RunRequest],
        clock: Callable[[], datetime] = datetime.now,
        on_problem: Callable[[Action, str], None] | None = None,
    ) -> None:
        self._store = store
        self._runs = runs
        self._request_for = request_for
        self._clock = clock
        # Called when a scheduled action did not happen, so it can be recorded and alerted on
        self._on_problem = on_problem
        self._watched: _Watched | None = None
        self.notices: list[str] = []

    async def run_forever(self) -> None:
        """Poll the wall clock; a long sleep would overshoot after the PC wakes up."""
        while True:
            try:
                self.tick()
            except Exception as exc:
                logger.exception("Scheduler tick failed")
                self._notice(
                    f"The scheduler hit an error ({describe_error(exc)}), so scheduled runs "
                    "may not start. See the terminal log."
                )
            await asyncio.sleep(POLL_SECONDS)

    def tick(self) -> None:
        now = self._clock()
        self._follow_up(now)
        due = due_actions(self._store.load(), now)
        for index, item in enumerate(due):
            if not self._handle(item, superseded=index < len(due) - 1):
                return

    def _handle(self, item: Due, superseded: bool) -> bool:
        """Deal with one due occurrence; return False to look at it again on the next tick."""
        when = _when(item.occurrence)
        if item.missed and (item.action is Action.DISABLE or superseded):
            # Stopping things late could interrupt someone; starting things after the next
            # disable time has passed would just undo the weekend. Report both instead.
            self._mark_handled(item)
            self._problem(
                item.action, f"Missed the scheduled {item.action.value} for {when}; it was not run."
            )
            return True
        if not self._runs_free(item.action):
            return False
        try:
            request = self._request_for(item.action)
        except WeekendSaverError as exc:
            # Not marked as handled: fixing the resource list in time should still let it run
            self._problem(
                item.action, f"The scheduled {item.action.value} for {when} cannot start: {exc}"
            )
            return False
        # Marked before starting. If this write fails nothing starts, which is better than
        # starting the same occurrence again on every tick.
        self._mark_handled(item)
        run_id = self._runs.start(request, "missed-schedule" if item.missed else "schedule")
        self._watched = _Watched(run_id, item.action, item.occurrence, attempts=1)
        if item.missed:
            self._notice(
                f"The scheduled {item.action.value} for {when} was missed and is running late."
            )
        return True

    def _runs_free(self, action: Action) -> bool:
        if not self._runs.busy:
            return True
        if self._runs.awaiting_approval:
            # A dialog nobody answered must not hold up the schedule
            self._runs.answer(False)
            self._notice(
                "A plan left waiting for approval was rejected so the scheduled "
                f"{action.value} could run."
            )
        return False

    def _follow_up(self, now: datetime) -> None:
        """Retry a scheduled enable that failed; a failed disable only costs a saving."""
        watched = self._watched
        if watched is None:
            return
        if watched.retry_at is None:
            record = self._runs.result_of(watched.run_id)
            if record is None:
                return
            self._watched = self._after_finished(watched, bool(record["failed"]), now)
            return
        if now < watched.retry_at or not self._runs_free(watched.action):
            return
        try:
            request = self._request_for(watched.action)
        except WeekendSaverError as exc:
            self._notice(f"Retrying the scheduled {watched.action.value} is not possible: {exc}")
            self._watched = replace(watched, retry_at=now + RETRY_INTERVAL)
            return
        run_id = self._runs.start(request, "schedule-retry")
        self._watched = replace(
            watched, run_id=run_id, attempts=watched.attempts + 1, retry_at=None
        )

    def _after_finished(self, watched: _Watched, failed: bool, now: datetime) -> _Watched | None:
        if not failed:
            return None
        action, when = watched.action.value, _when(watched.occurrence)
        if watched.action is Action.ENABLE and watched.attempts < MAX_ENABLE_ATTEMPTS:
            minutes = int(RETRY_INTERVAL.total_seconds() // 60)
            self._notice(
                f"The scheduled {action} for {when} failed (attempt {watched.attempts} of "
                f"{MAX_ENABLE_ATTEMPTS}); trying again in {minutes} minutes."
            )
            return replace(watched, retry_at=now + RETRY_INTERVAL)
        self._notice(
            f"The scheduled {action} for {when} FAILED and will not be retried. "
            "Open it under Recent runs and act on what is still wrong."
        )
        return None

    def _mark_handled(self, item: Due) -> None:
        fired = {item.action.value: item.occurrence.isoformat()}
        self._store.update(lambda state: replace(state, last_fired={**state.last_fired, **fired}))

    def _problem(self, action: Action, message: str) -> None:
        if not self._notice(message) or self._on_problem is None:
            return
        try:
            self._on_problem(action, message)
        except Exception:
            logger.exception("Could not record a scheduler problem")

    def _notice(self, message: str) -> bool:
        """Show `message` in the dashboard; return False if it repeats the latest notice."""
        if self.notices and self.notices[-1] == message:
            return False
        logger.warning(message)
        self.notices = [*self.notices, message][-MAX_NOTICES:]
        return True


def _when(occurrence: datetime) -> str:
    return occurrence.strftime("%A %H:%M")
