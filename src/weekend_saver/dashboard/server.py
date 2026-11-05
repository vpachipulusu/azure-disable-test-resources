"""Wire the dashboard's services together and serve it on loopback."""

import asyncio
import logging
import secrets
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import uvicorn

from weekend_saver.auth import AUTH_RECORD_FILE, SignedInCredential
from weekend_saver.azure_handlers import OPERATIONS, build_azure_handlers
from weekend_saver.config import load_resource_specs, save_resource_specs
from weekend_saver.dashboard.app import DashboardServices, create_app
from weekend_saver.dashboard.demo import DEMO_SELECTION, DemoAuth, DemoDiscovery, demo_handlers
from weekend_saver.dashboard.history import RunHistory
from weekend_saver.dashboard.run_manager import RunManager
from weekend_saver.dashboard.scheduler import Scheduler, ScheduleStore
from weekend_saver.discovery import AzureDiscovery
from weekend_saver.executors import DEFAULT_VERIFY_INTERVAL_SECONDS
from weekend_saver.files import ensure_private_dir
from weekend_saver.models import Action, RunReport, RunRequest, RunStatus
from weekend_saver.notifier import Notifier
from weekend_saver.state_store import JsonFileStateStore
from weekend_saver.workflow import build_workflow

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
RESOURCES_FILE = "resources.yaml"
DEMO_VERIFY_INTERVAL_SECONDS = 0.4
OPEN_BROWSER_DELAY_SECONDS = 1.0


def build_services(
    *,
    data_dir: Path,
    resources_path: Path | None = None,
    demo: bool = False,
    notifier: Notifier | None = None,
) -> DashboardServices:
    """Build everything the dashboard needs; `demo` swaps Azure for an in-memory fake.

    The resource selection lives in `data_dir` unless `resources_path` says otherwise.
    """
    if demo:
        # A throwaway folder, so trying the demo never touches real state or selections
        data_dir = Path(tempfile.mkdtemp(prefix="weekend-saver-demo-"))
        resources_path = data_dir / RESOURCES_FILE
        save_resource_specs(resources_path, DEMO_SELECTION)
        handlers = demo_handlers()
        auth = DemoAuth()
        discovery = DemoDiscovery()
        verify_interval = DEMO_VERIFY_INTERVAL_SECONDS
    else:
        ensure_private_dir(data_dir)
        resources_path = resources_path or data_dir / RESOURCES_FILE
        credential = SignedInCredential(data_dir / AUTH_RECORD_FILE)
        handlers = build_azure_handlers(credential)
        auth = credential
        discovery = AzureDiscovery(credential, frozenset(OPERATIONS))
        verify_interval = DEFAULT_VERIFY_INTERVAL_SECONDS

    state_store = JsonFileStateStore(data_dir / "state.json")
    history = RunHistory(data_dir / "runs.jsonl")
    schedule_store = ScheduleStore(data_dir / "schedule.json")

    def workflow_factory():
        return build_workflow(
            handlers, state_store, notifier=notifier, verify_interval_seconds=verify_interval
        )

    def scheduled_request(action: Action) -> RunRequest:
        # Nobody is watching a scheduled run, so it is approved up front
        return RunRequest(action, load_resource_specs(resources_path), auto_approve=True)

    runs = RunManager(workflow_factory, history)
    scheduler = Scheduler(
        schedule_store,
        runs,
        request_for=scheduled_request,
        on_problem=_problem_recorder(history, notifier),
    )
    return DashboardServices(
        auth=auth,
        discovery=discovery,
        handlers=handlers,
        state_store=state_store,
        resources_path=resources_path,
        workflow_factory=workflow_factory,
        runs=runs,
        schedule_store=schedule_store,
        scheduler=scheduler,
        history=history,
        demo=demo,
    )


def _problem_recorder(
    history: RunHistory, notifier: Notifier | None
) -> Callable[[Action, str], None]:
    """Make a scheduled action that never ran as visible as one that ran and failed."""
    alerts: set[asyncio.Task[None]] = set()

    def record(action: Action, message: str) -> None:
        now = datetime.now().isoformat()
        history.append(
            {
                "id": uuid.uuid4().hex[:12],
                "action": action.value,
                "trigger": "schedule",
                "dry_run": False,
                "started_at": now,
                "finished_at": now,
                "status": "not_run",
                "failed": True,
                "error": message,
                "results": [],
            }
        )
        if notifier is None:
            return
        # Called from the scheduler's tick, which runs on the event loop
        alert = asyncio.ensure_future(
            notifier.notify(RunReport(action, RunStatus.INVALID, error=message))
        )
        alerts.add(alert)
        alert.add_done_callback(alerts.discard)
        alert.add_done_callback(_log_failed_alert)

    return record


def _log_failed_alert(alert: "asyncio.Task[None]") -> None:
    if not alert.cancelled() and alert.exception() is not None:
        logger.error("Could not send a scheduler alert", exc_info=alert.exception())


def serve(services: DashboardServices, *, port: int, open_browser: bool) -> None:
    """Serve until interrupted. The printed link works once and sets the session cookie."""
    code = secrets.token_urlsafe(32)
    url = f"http://{LOOPBACK}:{port}/?token={code}"
    sys.stdout.write(f"weekend-saver dashboard: {url}\n(the link works once; Ctrl+C to stop)\n")
    sys.stdout.flush()
    if open_browser:
        timer = threading.Timer(OPEN_BROWSER_DELAY_SECONDS, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    # No access log: the first request's URL contains the one-time code
    uvicorn.run(
        create_app(services, code, port=port),
        host=LOOPBACK,
        port=port,
        log_level="warning",
        access_log=False,
    )
