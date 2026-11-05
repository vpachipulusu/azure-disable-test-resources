"""The dashboard's HTTP API.

This server can stop and start Azure resources as the signed-in person, so it only
answers on loopback, the link printed at start-up works once, and every /api call must
carry the session cookie that link set. Changing anything additionally needs a custom
header, which a cross-site page cannot send.
"""

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from agent_framework import Workflow
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from weekend_saver.arm import build_targets, parse_resource_id
from weekend_saver.auth import NotSignedInError, SignInStatus
from weekend_saver.config import load_resource_specs, save_resource_specs
from weekend_saver.dashboard.history import RunHistory
from weekend_saver.dashboard.run_manager import (
    RESYNC,
    NoPendingApprovalError,
    RunInProgressError,
    RunManager,
)
from weekend_saver.dashboard.scheduler import (
    Schedule,
    Scheduler,
    ScheduleState,
    ScheduleStore,
    WeeklyTime,
    next_occurrence,
)
from weekend_saver.dashboard.serialize import short_name
from weekend_saver.dashboard.views import managed_rows, workflow_graph
from weekend_saver.discovery import DiscoveredResource, Discovery
from weekend_saver.errors import ConfigError, WeekendSaverError
from weekend_saver.executors import describe_error
from weekend_saver.handlers import ResourceHandler
from weekend_saver.models import Action, ResourceSpec, RunRequest
from weekend_saver.state_store import StateStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CHANGE_HEADER = "x-weekend-saver"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
KEEP_ALIVE_SECONDS = 15.0
# The page loads nothing from other origins and has no inline script or style
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

_NOT_OPENED_FROM_CLI = (
    "<!doctype html><title>weekend-saver</title>"
    "<p>Open the dashboard with the link printed in the terminal that started it. "
    "The link works once; restart the dashboard for a new one.</p>"
)


def session_cookie_name(port: int) -> str:
    # Browsers share cookies across ports on one host, so each dashboard port gets its own
    return f"weekend_saver_session_{port}"


class AuthService(Protocol):
    def status(self) -> SignInStatus: ...

    def login(self) -> SignInStatus: ...


@dataclass(frozen=True)
class DashboardServices:
    auth: AuthService
    discovery: Discovery
    handlers: Mapping[str, ResourceHandler]
    state_store: StateStore
    resources_path: Path
    workflow_factory: Callable[[], Workflow]
    runs: RunManager
    schedule_store: ScheduleStore
    scheduler: Scheduler
    history: RunHistory
    demo: bool = False
    clock: Callable[[], datetime] = datetime.now


class ResourceSelection(BaseModel):
    id: str = Field(min_length=1, max_length=1024)
    order: int = Field(default=0, ge=-1000, le=1000)


class SaveResourcesBody(BaseModel):
    resources: list[ResourceSelection] = Field(max_length=2000)


class StartRunBody(BaseModel):
    action: Literal["disable", "enable"]
    dry_run: bool = False
    ignore_prior_state: bool = False


class ApprovalBody(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    approved: bool


class WeeklyTimeBody(BaseModel):
    weekday: int = Field(ge=0, le=6)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    def to_weekly_time(self) -> WeeklyTime:
        hour, minute = self.time.split(":")
        return WeeklyTime(self.weekday, int(hour), int(minute))


class ScheduleBody(BaseModel):
    enabled: bool
    disable_at: WeeklyTimeBody
    enable_at: WeeklyTimeBody


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"success": True, "data": data, "error": None}, status_code=status_code)


def fail(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": message}, status_code=status_code)


async def event_stream(
    runs: RunManager, keep_alive_seconds: float = KEEP_ALIVE_SECONDS
) -> AsyncIterator[str]:
    """Server-Sent Events: the current run first, then everything as it happens."""
    queue = runs.subscribe()
    try:
        yield _sse({"type": "snapshot", "run": runs.current()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keep_alive_seconds)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is RESYNC:
                # This client fell behind and its backlog was dropped; start it over
                event = {"type": "snapshot", "run": runs.current()}
            yield _sse(event)
    finally:
        runs.unsubscribe(queue)


def _sse(event: Mapping[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def create_app(
    services: DashboardServices,
    bootstrap_token: str,
    *,
    port: int,
    session_id: str | None = None,
) -> FastAPI:
    """Build the app. `bootstrap_token` is the one-time code in the printed link."""
    session = session_id or secrets.token_urlsafe(32)
    cookie_name = session_cookie_name(port)
    allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    bootstrap = {"used": False}
    login_in_progress = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler_task = asyncio.create_task(services.scheduler.run_forever())
        try:
            yield
        finally:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
            await services.runs.shutdown()

    app = FastAPI(
        title="weekend-saver", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    graph = workflow_graph(services.workflow_factory())

    def has_session(request: Request) -> bool:
        return _same(request.cookies.get(cookie_name, ""), session)

    def refusal(request: Request) -> JSONResponse | None:
        # A hostile web page can make the browser call localhost, and DNS rebinding can
        # make that look same-origin. The exact Host check stops rebinding; the cookie and
        # the custom header stop everything else.
        host = request.headers.get("host", "").lower()
        if host not in allowed_hosts:
            return fail(403, "The dashboard only answers on localhost.")
        if not request.url.path.startswith("/api/"):
            return None
        if not has_session(request):
            return fail(401, "Open the dashboard with the link printed in the terminal.")
        if request.method not in SAFE_METHODS and not _is_own_page(request, host):
            return fail(403, "Changes are only accepted from the dashboard page itself.")
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> FastAPIResponse:
        response = refusal(request) or await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
            for error in exc.errors()
        )
        return fail(422, f"Invalid request: {problems}")

    @app.exception_handler(WeekendSaverError)
    async def expected_failure(_: Request, exc: WeekendSaverError) -> JSONResponse:
        if isinstance(exc, RunInProgressError | NoPendingApprovalError):
            return fail(409, str(exc))
        if isinstance(exc, NotSignedInError):
            return fail(403, str(exc))
        if isinstance(exc, ConfigError):
            return fail(400, str(exc))
        logger.exception("Dashboard request failed", exc_info=exc)
        return fail(500, str(exc))

    @app.exception_handler(Exception)
    async def unexpected_failure(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Dashboard request crashed", exc_info=exc)
        return fail(500, f"Unexpected error ({describe_error(exc)}); see the terminal log.")

    @app.get("/", response_model=None)
    async def index(request: Request, token: str = "") -> FastAPIResponse:
        if token:
            if bootstrap["used"] or not _same(token, bootstrap_token):
                return HTMLResponse(_NOT_OPENED_FROM_CLI, status_code=401)
            # The link's code is spent here and never becomes the session secret, so a copy
            # of it left in browser history or a log is useless afterwards.
            bootstrap["used"] = True
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie_name, session, httponly=True, samesite="strict", path="/")
            return response
        if not has_session(request):
            return HTMLResponse(_NOT_OPENED_FROM_CLI, status_code=401)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/auth")
    async def auth_status() -> JSONResponse:
        status = await asyncio.to_thread(services.auth.status)
        return ok({**asdict(status), "demo": services.demo})

    @app.post("/api/auth/login")
    async def login() -> JSONResponse:
        if login_in_progress.locked():
            return fail(409, "A sign-in is already in progress; finish it in the browser window.")
        async with login_in_progress:
            try:
                status = await asyncio.to_thread(services.auth.login)
            except Exception as exc:
                logger.exception("Browser sign-in failed")
                return fail(
                    400, f"Sign-in did not complete ({describe_error(exc)}). Try `az login`."
                )
        return ok({**asdict(status), "demo": services.demo})

    @app.get("/api/discovery")
    async def discovery() -> JSONResponse:
        specs = await asyncio.to_thread(_specs_or_empty)
        found = await services.discovery.list()
        return ok(_picker_rows(found, specs))

    @app.get("/api/resources")
    async def resources() -> JSONResponse:
        specs = await asyncio.to_thread(_specs_or_empty)
        rows = await managed_rows(specs, services.handlers, services.state_store)
        return ok(rows)

    @app.put("/api/resources")
    def save_resources(body: SaveResourcesBody) -> JSONResponse:
        specs = tuple(ResourceSpec(item.id, item.order) for item in body.resources)
        if specs:
            build_targets(specs, frozenset(services.handlers))
        save_resource_specs(services.resources_path, specs)
        return ok({"saved": len(specs)})

    @app.get("/api/workflow")
    async def workflow() -> JSONResponse:
        return ok(graph)

    @app.get("/api/runs")
    def runs() -> JSONResponse:
        return ok({"current": services.runs.current(), "recent": services.history.recent()})

    @app.post("/api/runs")
    async def start_run(body: StartRunBody) -> JSONResponse:
        action = Action(body.action)
        request = RunRequest(
            action=action,
            resources=await asyncio.to_thread(load_resource_specs, services.resources_path),
            dry_run=body.dry_run,
            ignore_prior_state=body.ignore_prior_state and action is Action.ENABLE,
        )
        return ok({"run_id": services.runs.start(request, trigger="manual")}, status_code=202)

    @app.post("/api/runs/approval")
    async def answer_approval(body: ApprovalBody) -> JSONResponse:
        services.runs.answer(body.approved, run_id=body.run_id)
        return ok({"approved": body.approved})

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(services.runs),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/schedule")
    def get_schedule() -> JSONResponse:
        return ok(_schedule_view(services.schedule_store.load()))

    @app.put("/api/schedule")
    def save_schedule(body: ScheduleBody) -> JSONResponse:
        schedule = Schedule(
            body.enabled, body.disable_at.to_weekly_time(), body.enable_at.to_weekly_time()
        )
        now = services.clock().isoformat()

        def change(current: ScheduleState) -> ScheduleState:
            if schedule == current.schedule:
                return current
            # Re-arm on any change so occurrences from before the edit never fire late
            return ScheduleState(
                schedule, armed_at=now if schedule.enabled else None, last_fired={}
            )

        return ok(_schedule_view(services.schedule_store.update(change)))

    def _specs_or_empty() -> tuple[ResourceSpec, ...]:
        if not services.resources_path.exists():
            return ()
        return load_resource_specs(services.resources_path)

    def _schedule_view(state: ScheduleState) -> dict[str, Any]:
        schedule = state.schedule
        now = services.clock()
        upcoming = {
            action.value: next_occurrence(now, schedule.time_for(action)).isoformat()
            if schedule.enabled
            else None
            for action in (Action.DISABLE, Action.ENABLE)
        }
        return {
            "enabled": schedule.enabled,
            "disable_at": _weekly_time_view(schedule.disable_at),
            "enable_at": _weekly_time_view(schedule.enable_at),
            "next": upcoming,
            "notices": list(services.scheduler.notices),
        }

    return app


def _picker_rows(
    found: tuple[DiscoveredResource, ...], specs: tuple[ResourceSpec, ...]
) -> list[dict[str, Any]]:
    """Everything discovered, plus managed resources discovery did not return.

    The picker saves exactly the rows it shows as ticked, so a managed resource that is
    missing here (not visible right now, or Resource Graph lagging) would otherwise be
    dropped from the selection without anyone choosing that.
    """
    managed = {spec.resource_id.lower(): spec for spec in specs}
    rows = [
        {
            **asdict(resource),
            "tags": dict(resource.tags),
            "managed": resource.resource_id.lower() in managed,
            "order": managed[resource.resource_id.lower()].order
            if resource.resource_id.lower() in managed
            else 0,
            "found": True,
        }
        for resource in found
    ]
    # Keep each resource group together, selectable resources before greyed-out ones
    rows.sort(
        key=lambda row: (
            row["subscription_name"].lower(),
            row["resource_group"].lower(),
            not row["supported"],
            row["name"].lower(),
        )
    )
    discovered = {resource.resource_id.lower() for resource in found}
    rows.extend(_missing_row(spec) for key, spec in managed.items() if key not in discovered)
    return rows


def _missing_row(spec: ResourceSpec) -> dict[str, Any]:
    row = {
        "resource_id": spec.resource_id,
        "name": short_name(spec.resource_id),
        "resource_type": "",
        "resource_group": "",
        "subscription_id": "",
        "subscription_name": "Not found right now",
        "location": "",
        "tags": {},
        "supported": True,
        "managed": True,
        "order": spec.order,
        "found": False,
    }
    with suppress(ConfigError):
        target = parse_resource_id(spec.resource_id)
        row = {
            **row,
            "resource_type": target.resource_type,
            "resource_group": target.resource_group,
            "subscription_id": target.subscription_id,
        }
    return row


def _weekly_time_view(at: WeeklyTime) -> dict[str, Any]:
    return {"weekday": at.weekday, "time": f"{at.hour:02d}:{at.minute:02d}"}


def _same(candidate: str, secret: str) -> bool:
    # Bytes, because compare_digest raises on non-ASCII text
    return secrets.compare_digest(candidate.encode(), secret.encode())


def _is_own_page(request: Request, host: str) -> bool:
    if request.headers.get(CHANGE_HEADER) != "1":
        return False
    origin = request.headers.get("origin")
    return origin is None or origin.lower() == f"http://{host}"
