import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from tests.helpers import SUBSCRIPTION_ID, VM_TYPE, FakeHandler, vm_id
from weekend_saver.auth import SignInStatus
from weekend_saver.config import load_resource_specs, save_resource_specs
from weekend_saver.dashboard.app import (
    CHANGE_HEADER,
    DashboardServices,
    create_app,
    event_stream,
    session_cookie_name,
)
from weekend_saver.dashboard.app import (
    _same as secrets_match,
)
from weekend_saver.dashboard.history import RunHistory
from weekend_saver.dashboard.run_manager import RunManager
from weekend_saver.dashboard.scheduler import Scheduler, ScheduleStore
from weekend_saver.discovery import DiscoveredResource
from weekend_saver.models import PowerState, ResourceSpec, RunRequest
from weekend_saver.state_store import JsonFileStateStore, Snapshot
from weekend_saver.workflow import build_workflow

TOKEN = "one-time-link-code"
SESSION = "test-session-id"
PORT = 8765
COOKIE = session_cookie_name(PORT)
BASE_URL = f"http://127.0.0.1:{PORT}"
CHANGE = {CHANGE_HEADER: "1"}
RUNNING, STOPPED = PowerState.RUNNING, PowerState.STOPPED
NOW = datetime(2026, 11, 4, 12, 0)  # a Wednesday


class FakeAuth:
    def __init__(self) -> None:
        self.signed_in = True
        self.login_error: Exception | None = None

    def status(self) -> SignInStatus:
        if not self.signed_in:
            return SignInStatus(False)
        return SignInStatus(True, "me@example.com", "tenant-1", "azure-cli")

    def login(self) -> SignInStatus:
        if self.login_error:
            raise self.login_error
        self.signed_in = True
        return self.status()


class FakeDiscovery:
    async def list(self) -> tuple[DiscoveredResource, ...]:
        return tuple(
            DiscoveredResource(
                vm_id(name), name, VM_TYPE, "rg-test", SUBSCRIPTION_ID, "Test Sub", "westeurope"
            )
            for name in ("app", "db", "other")
        )


class Dashboard:
    """A running dashboard wired to fakes, plus handles on the fakes."""

    def __init__(self, tmp_path, handler: FakeHandler) -> None:
        self.handler = handler
        self.auth = FakeAuth()
        self.resources_path = tmp_path / "resources.yaml"
        self.state_store = JsonFileStateStore(tmp_path / "state.json")
        self.history = RunHistory(tmp_path / "runs.jsonl")
        handlers = {VM_TYPE: handler}

        def workflow_factory():
            return build_workflow(
                handlers, self.state_store, verify_attempts=2, verify_interval_seconds=0
            )

        runs = RunManager(workflow_factory, self.history, clock=lambda: NOW)
        schedule_store = ScheduleStore(tmp_path / "schedule.json")
        self.services = DashboardServices(
            auth=self.auth,
            discovery=FakeDiscovery(),
            handlers=handlers,
            state_store=self.state_store,
            resources_path=self.resources_path,
            workflow_factory=workflow_factory,
            runs=runs,
            schedule_store=schedule_store,
            scheduler=Scheduler(
                schedule_store, runs, request_for=lambda action: RunRequest(action, ())
            ),
            history=self.history,
            clock=lambda: NOW,
        )
        self.app = create_app(self.services, TOKEN, port=PORT, session_id=SESSION)

    def manage(self, *names: str) -> None:
        save_resource_specs(self.resources_path, tuple(ResourceSpec(vm_id(n)) for n in names))


@pytest.fixture
def dashboard(tmp_path):
    return Dashboard(tmp_path, FakeHandler({"app": RUNNING, "db": STOPPED, "other": RUNNING}))


@pytest.fixture
def client(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as test_client:
        test_client.cookies.set(COOKIE, SESSION)
        yield test_client


def data(response):
    body = response.json()
    assert body["success"], body
    return body["data"]


def wait_for(client: TestClient, condition, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = data(client.get("/api/runs"))
        if condition(runs):
            return runs
        time.sleep(0.02)
    raise AssertionError("the run did not reach the expected state in time")


# --- access control ---------------------------------------------------------------------


def test_api_refuses_requests_without_the_session_cookie(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL) as anonymous:
        response = anonymous.get("/api/auth")

    assert response.status_code == 401
    assert response.json()["success"] is False


def test_index_refuses_visitors_who_did_not_come_from_the_cli_link(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as anonymous:
        assert anonymous.get("/").status_code == 401
        assert anonymous.get("/?token=wrong").status_code == 401


def test_cli_link_swaps_the_token_for_a_cookie_and_serves_the_page(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as fresh:
        redirect = fresh.get(f"/?token={TOKEN}")
        cookie = redirect.headers["set-cookie"]
        page = fresh.get("/")

    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/"
    assert "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()
    assert page.status_code == 200
    assert "weekend-saver" in page.text
    # The session secret is not the code from the link
    assert TOKEN not in cookie
    assert SESSION in cookie


def test_cli_link_works_only_once(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as first:
        assert first.get(f"/?token={TOKEN}").status_code == 303
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as replay:
        again = replay.get(f"/?token={TOKEN}")

    assert again.status_code == 401
    assert "set-cookie" not in again.headers


@pytest.mark.parametrize("value", ["é", "x" * 5000, ""])
def test_odd_tokens_and_cookies_are_just_refused(dashboard, value):
    with TestClient(dashboard.app, base_url=BASE_URL, follow_redirects=False) as odd:
        page = odd.get("/", params={"token": value} if value else None)
        api = odd.get("/api/auth")

    assert page.status_code == 401
    assert api.status_code == 401


def test_secret_comparison_copes_with_non_ascii_values():
    # A browser can send such a cookie even though the test client cannot
    assert not secrets_match("é", SESSION)
    assert secrets_match(SESSION, SESSION)


@pytest.mark.parametrize(
    "host", ["127.0.0.1:9999", "127.0.0.1", "localhost.:8765", "127.0.0.1.evil.example:8765"]
)
def test_host_must_match_the_served_address_exactly(dashboard, host):
    with TestClient(dashboard.app, base_url=BASE_URL) as other:
        other.cookies.set(COOKIE, SESSION)
        response = other.get("/api/auth", headers={"Host": host})

    assert response.status_code == 403
    assert "Content-Security-Policy" in response.headers


def test_localhost_name_is_accepted_and_schema_is_not_published(dashboard):
    with TestClient(dashboard.app, base_url=f"http://localhost:{PORT}") as local:
        local.cookies.set(COOKIE, SESSION)

        assert local.get("/api/auth").status_code == 200
        assert local.get("/openapi.json").status_code == 404


def test_requests_for_another_host_name_are_refused(dashboard):
    with TestClient(dashboard.app, base_url="http://evil.example:8765") as rebinding:
        rebinding.cookies.set(COOKIE, SESSION)
        response = rebinding.get("/api/auth")

    assert response.status_code == 403


def test_changes_need_the_dashboard_header(client):
    response = client.post("/api/runs", json={"action": "disable"})

    assert response.status_code == 403


def test_changes_from_another_origin_are_refused(client):
    response = client.post(
        "/api/runs",
        json={"action": "disable"},
        headers={**CHANGE, "Origin": "http://evil.example"},
    )

    assert response.status_code == 403


# --- sign-in, discovery, resources ------------------------------------------------------


def test_auth_reports_who_is_signed_in(client):
    status = data(client.get("/api/auth"))

    assert (status["signed_in"], status["account"], status["demo"]) == (
        True,
        "me@example.com",
        False,
    )


def test_login_failure_is_reported_without_internal_detail(client, dashboard):
    dashboard.auth.login_error = RuntimeError("AADSTS50020 tenant secret detail")

    response = client.post("/api/auth/login", headers=CHANGE)

    assert response.status_code == 400
    assert "RuntimeError" in response.json()["error"]
    assert "AADSTS50020" not in response.json()["error"]


def test_login_success_returns_the_new_status(client, dashboard):
    dashboard.auth.signed_in = False

    status = data(client.post("/api/auth/login", headers=CHANGE))

    assert status["signed_in"] is True


def test_discovery_marks_which_resources_are_already_managed(client, dashboard):
    dashboard.manage("app")

    found = {resource["name"]: resource for resource in data(client.get("/api/discovery"))}

    assert found["app"]["managed"] is True
    assert found["app"]["found"] is True
    assert found["db"]["managed"] is False
    assert found["app"]["subscription_name"] == "Test Sub"


def test_discovery_keeps_managed_resources_it_could_not_find(client, dashboard):
    dashboard.manage("app", "gone")

    found = {resource["name"]: resource for resource in data(client.get("/api/discovery"))}

    assert found["gone"]["managed"] is True
    assert found["gone"]["found"] is False
    assert found["gone"]["resource_id"] == vm_id("gone")
    assert found["gone"]["resource_group"] == "rg-test"


def test_a_second_sign_in_while_one_is_open_is_refused(tmp_path):
    import threading

    release = threading.Event()
    entered = threading.Event()
    dashboard = Dashboard(tmp_path, FakeHandler({}))

    def slow_login():
        entered.set()
        release.wait(timeout=5)
        return dashboard.auth.status()

    dashboard.auth.login = slow_login
    with TestClient(dashboard.app, base_url=BASE_URL) as test_client:
        test_client.cookies.set(COOKIE, SESSION)
        first: list = []
        worker = threading.Thread(
            target=lambda: first.append(test_client.post("/api/auth/login", headers=CHANGE))
        )
        worker.start()
        assert entered.wait(timeout=5)
        second = test_client.post("/api/auth/login", headers=CHANGE)
        release.set()
        worker.join(timeout=5)

    assert second.status_code == 409
    assert first[0].status_code == 200


def test_saving_a_selection_writes_the_resource_file(client, dashboard):
    body = {"resources": [{"id": vm_id("app")}, {"id": vm_id("db"), "order": 10}]}

    saved = data(client.put("/api/resources", json=body, headers=CHANGE))

    assert saved == {"saved": 2}
    assert load_resource_specs(dashboard.resources_path) == (
        ResourceSpec(vm_id("app"), 0),
        ResourceSpec(vm_id("db"), 10),
    )


@pytest.mark.parametrize(
    "resource_id",
    [
        "not-an-arm-id",
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/x",
    ],
)
def test_saving_an_invalid_or_unsupported_resource_is_refused(client, dashboard, resource_id):
    response = client.put(
        "/api/resources", json={"resources": [{"id": resource_id}]}, headers=CHANGE
    )

    assert response.status_code == 400
    assert not dashboard.resources_path.exists()


def test_clearing_the_selection_is_allowed(client, dashboard):
    dashboard.manage("app")

    data(client.put("/api/resources", json={"resources": []}, headers=CHANGE))

    assert load_resource_specs(dashboard.resources_path) == ()


def test_resource_table_shows_live_state_and_what_we_disabled(client, dashboard):
    dashboard.manage("app", "db")
    dashboard.state_store.save(Snapshot({vm_id("db").lower(): "2026-11-06T19:00:00"}))

    rows = {row["name"]: row for row in data(client.get("/api/resources"))}

    assert (rows["app"]["state"], rows["app"]["disabled_by_us"]) == ("running", False)
    assert (rows["db"]["state"], rows["db"]["disabled_by_us"]) == ("stopped", True)
    assert rows["app"]["resource_group"] == "rg-test"


def test_resource_table_is_empty_before_anything_is_selected(client):
    assert data(client.get("/api/resources")) == []


def test_unreadable_resource_shows_an_error_on_its_own_row(tmp_path):
    handler = FakeHandler({"app": RUNNING, "blind": RUNNING}, fail_status_on={"blind"})
    dashboard = Dashboard(tmp_path, handler)
    dashboard.manage("app", "blind")
    with TestClient(dashboard.app, base_url=BASE_URL) as test_client:
        test_client.cookies.set(COOKIE, SESSION)
        rows = {row["name"]: row for row in data(test_client.get("/api/resources"))}

    assert rows["app"]["error"] == ""
    assert rows["blind"]["error"] == "RuntimeError"
    assert rows["blind"]["state"] == "unknown"


# --- workflow graph and runs ------------------------------------------------------------


def test_workflow_graph_matches_the_built_workflow(client):
    graph = data(client.get("/api/workflow"))

    assert [node["id"] for node in graph["nodes"]] == [
        "load_targets",
        "resolve_state",
        "approval",
        "apply",
        "verify",
        "report",
    ]
    kinds = {(edge["from"], edge["to"]): edge["kind"] for edge in graph["edges"]}
    assert kinds[("approval", "apply")] == "main"
    assert kinds[("approval", "load_targets")] == "loop"
    assert kinds[("load_targets", "report")] == "skip"


def test_run_waits_for_approval_then_completes_and_lands_in_history(client, dashboard):
    dashboard.manage("app")

    started = client.post("/api/runs", json={"action": "disable"}, headers=CHANGE)
    waiting = wait_for(client, lambda runs: runs["current"] and runs["current"]["pending_plan"])
    busy = client.post("/api/runs", json={"action": "disable"}, headers=CHANGE)
    wrong_run = client.post(
        "/api/runs/approval", json={"run_id": "another-run", "approved": True}, headers=CHANGE
    )
    answer = {"run_id": waiting["current"]["id"], "approved": True}
    data(client.post("/api/runs/approval", json=answer, headers=CHANGE))
    finished = wait_for(client, lambda runs: runs["current"] is None)

    assert started.status_code == 202
    assert waiting["current"]["pending_plan"]["items"][0]["name"] == "app"
    assert busy.status_code == 409
    assert wrong_run.status_code == 409
    assert dashboard.handler.calls == [("disable", "app")]
    latest = finished["recent"][0]
    assert (latest["status"], latest["trigger"], latest["id"]) == (
        "completed",
        "manual",
        data(started)["run_id"],
    )


def test_dry_run_finishes_without_approval_or_changes(client, dashboard):
    dashboard.manage("app")

    client.post("/api/runs", json={"action": "disable", "dry_run": True}, headers=CHANGE)
    finished = wait_for(client, lambda runs: runs["current"] is None and runs["recent"])

    assert finished["recent"][0]["status"] == "dry_run"
    assert dashboard.handler.calls == []


def test_starting_a_run_without_a_resource_list_is_a_client_error(client):
    response = client.post("/api/runs", json={"action": "disable"}, headers=CHANGE)

    assert response.status_code == 400
    assert "not found" in response.json()["error"]


def test_answering_when_nothing_is_pending_is_a_conflict(client):
    response = client.post(
        "/api/runs/approval", json={"run_id": "none", "approved": True}, headers=CHANGE
    )

    assert response.status_code == 409


def test_unknown_action_is_rejected_by_validation(client):
    response = client.post("/api/runs", json={"action": "delete"}, headers=CHANGE)

    assert response.status_code == 422
    assert response.json()["success"] is False


# --- schedule ---------------------------------------------------------------------------


def test_schedule_is_off_by_default(client):
    schedule = data(client.get("/api/schedule"))

    assert schedule["enabled"] is False
    assert schedule["disable_at"] == {"weekday": 4, "time": "19:00"}
    assert schedule["next"] == {"disable": None, "enable": None}


def test_turning_the_schedule_on_arms_it_and_shows_the_next_runs(client, dashboard):
    body = {
        "enabled": True,
        "disable_at": {"weekday": 4, "time": "18:30"},
        "enable_at": {"weekday": 0, "time": "07:00"},
    }

    schedule = data(client.put("/api/schedule", json=body, headers=CHANGE))

    assert schedule["next"] == {"disable": "2026-11-06T18:30:00", "enable": "2026-11-09T07:00:00"}
    stored = dashboard.services.schedule_store.load()
    assert stored.schedule.enabled
    assert stored.armed_at == NOW.isoformat()


def test_saving_an_unchanged_schedule_keeps_what_already_fired(client, dashboard):
    body = {
        "enabled": True,
        "disable_at": {"weekday": 4, "time": "19:00"},
        "enable_at": {"weekday": 0, "time": "06:00"},
    }
    client.put("/api/schedule", json=body, headers=CHANGE)
    store = dashboard.services.schedule_store
    fired = {"disable": "2026-11-06T19:00:00"}
    store.save(type(store.load())(store.load().schedule, store.load().armed_at, fired))

    client.put("/api/schedule", json=body, headers=CHANGE)

    assert store.load().last_fired == fired


def test_invalid_schedule_time_is_rejected(client):
    body = {
        "enabled": True,
        "disable_at": {"weekday": 4, "time": "25:00"},
        "enable_at": {"weekday": 0, "time": "06:00"},
    }

    assert client.put("/api/schedule", json=body, headers=CHANGE).status_code == 422


# --- event stream -----------------------------------------------------------------------


async def test_event_stream_sends_a_snapshot_then_live_events(dashboard):
    runs = dashboard.services.runs
    stream = event_stream(runs, keep_alive_seconds=0.05)

    first = await anext(stream)
    runs._publish({"type": "step", "step": "apply", "status": "running"})
    second = await anext(stream)
    keep_alive = await anext(stream)
    await stream.aclose()

    assert json.loads(first.removeprefix("data: ")) == {"type": "snapshot", "run": None}
    assert json.loads(second.removeprefix("data: "))["step"] == "apply"
    assert keep_alive.startswith(":")
    assert runs._subscribers == set()


def test_events_endpoint_needs_a_session(dashboard):
    with TestClient(dashboard.app, base_url=BASE_URL) as anonymous:
        assert anonymous.get("/api/events").status_code == 401
