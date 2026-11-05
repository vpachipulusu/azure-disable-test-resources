import asyncio
import os
import socket

import pytest

from weekend_saver.arm import parse_resource_id
from weekend_saver.config import load_resource_specs
from weekend_saver.dashboard import server
from weekend_saver.dashboard.demo import (
    DEMO_RESOURCES,
    DEMO_SELECTION,
    DemoAuth,
    DemoDiscovery,
    DemoHandler,
    demo_handlers,
)
from weekend_saver.dashboard.server import build_services, serve
from weekend_saver.errors import ConfigError
from weekend_saver.files import default_data_dir, ensure_private_dir
from weekend_saver.models import Action, PowerState


async def test_demo_handler_moves_resources_between_states():
    handler = DemoHandler(read_delay_seconds=0, change_delay_seconds=0)
    target = parse_resource_id(DEMO_SELECTION[0].resource_id)

    before = await handler.get_status(target)
    await handler.disable(target)
    stopped = await handler.get_status(target)
    await handler.enable(target)
    after = await handler.get_status(target)

    assert before.power_state is PowerState.RUNNING
    assert stopped.power_state is PowerState.STOPPED
    assert after.power_state is PowerState.RUNNING


async def test_demo_catalogue_is_consistent():
    handlers = demo_handlers(DemoHandler(read_delay_seconds=0, change_delay_seconds=0))
    discovered = await DemoDiscovery().list()

    unsupported = [resource for resource in discovered if not resource.supported]
    discovered = tuple(resource for resource in discovered if resource.supported)
    assert discovered == DEMO_RESOURCES
    assert unsupported
    assert all(resource.resource_type not in handlers for resource in unsupported)
    assert DemoAuth().login().signed_in
    for resource in discovered:
        target = parse_resource_id(resource.resource_id)
        assert target.resource_type == resource.resource_type
        assert (await handlers[target.resource_type].get_status(target)).tags == resource.tags
    assert {spec.resource_id for spec in DEMO_SELECTION} <= {r.resource_id for r in discovered}


def test_demo_services_use_a_throwaway_folder_and_a_seeded_selection(tmp_path):
    real_resources = tmp_path / "resources.yaml"

    services = build_services(resources_path=real_resources, data_dir=tmp_path, demo=True)

    assert services.demo
    assert services.resources_path != real_resources
    assert not real_resources.exists()
    assert load_resource_specs(services.resources_path) == DEMO_SELECTION
    assert isinstance(services.auth, DemoAuth)
    request = services.scheduler._request_for(Action.DISABLE)
    assert request.auto_approve
    assert request.resources == DEMO_SELECTION


def test_real_services_sign_in_with_the_users_account(tmp_path, monkeypatch):
    created = []

    class FakeCredential:
        def __init__(self, record_path) -> None:
            created.append(record_path)

    monkeypatch.setattr(server, "SignedInCredential", FakeCredential)
    monkeypatch.setattr(server, "build_azure_handlers", lambda credential: {})
    monkeypatch.setattr(server, "AzureDiscovery", lambda credential, types: ("discovery", types))

    services = build_services(resources_path=tmp_path / "resources.yaml", data_dir=tmp_path)

    assert created == [tmp_path / "auth-record.json"]
    assert isinstance(services.auth, FakeCredential)
    assert not services.demo
    assert services.resources_path == tmp_path / "resources.yaml"


def test_selection_lives_in_the_data_folder_unless_told_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SignedInCredential", lambda record_path: object())
    monkeypatch.setattr(server, "build_azure_handlers", lambda credential: {})
    monkeypatch.setattr(server, "AzureDiscovery", lambda credential, types: object())
    data_dir = tmp_path / "not" / "yet" / "there"

    services = build_services(data_dir=data_dir)

    assert data_dir.is_dir()
    assert services.resources_path == data_dir / "resources.yaml"


async def test_a_scheduled_action_that_never_ran_is_recorded_and_alerted(tmp_path):
    alerts = []

    class RecordingNotifier:
        async def notify(self, report) -> None:
            alerts.append(report)

    services = build_services(data_dir=tmp_path, demo=True, notifier=RecordingNotifier())

    services.scheduler._on_problem(Action.ENABLE, "Missed the scheduled enable")
    await asyncio.sleep(0)

    latest = services.history.recent()[0]
    assert (latest["status"], latest["failed"], latest["action"]) == ("not_run", True, "enable")
    assert latest["error"] == "Missed the scheduled enable"
    assert alerts[0].failed
    assert alerts[0].error == "Missed the scheduled enable"


def test_serve_refuses_a_port_that_is_taken_before_printing_a_link(tmp_path, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **options: started.append(options))
    services = build_services(data_dir=tmp_path, demo=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        taken_port = occupied.getsockname()[1]
        with pytest.raises(ConfigError, match=f"Port {taken_port} is already in use"):
            serve(services, port=taken_port, open_browser=True)

    assert started == []
    assert "token=" not in capsys.readouterr().out


def test_default_data_folder_is_per_user_not_the_project(tmp_path, monkeypatch):
    variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"
    monkeypatch.setenv(variable, str(tmp_path))

    assert default_data_dir() == tmp_path / "weekend-saver"
    assert ensure_private_dir(default_data_dir()).is_dir()


def test_serve_binds_to_loopback_and_keeps_the_token_out_of_the_logs(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **options: calls.append(options))
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setattr(server, "OPEN_BROWSER_DELAY_SECONDS", 0)
    services = build_services(resources_path=tmp_path / "r.yaml", data_dir=tmp_path, demo=True)

    serve(services, port=9123, open_browser=False)

    (options,) = calls
    printed = capsys.readouterr().out
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 9123
    assert options["access_log"] is False
    assert "http://127.0.0.1:9123/?token=" in printed
    assert opened == []
