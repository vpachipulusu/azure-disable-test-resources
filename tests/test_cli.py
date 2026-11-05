import builtins
from typing import ClassVar

import pytest

from tests.helpers import VM_TYPE, FakeHandler, vm_id
from weekend_saver import cli
from weekend_saver.auth import SignInStatus
from weekend_saver.models import PowerState

RUNNING, STOPPED = PowerState.RUNNING, PowerState.STOPPED


@pytest.fixture
def resources(tmp_path):
    path = tmp_path / "resources.yaml"
    path.write_text(f"resources:\n  - {vm_id('app')}\n", encoding="utf-8")
    return path


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state" / "state.json"


@pytest.fixture(autouse=True)
def no_webhook(monkeypatch):
    monkeypatch.delenv(cli.WEBHOOK_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def instant_verification(monkeypatch):
    monkeypatch.setattr(cli, "VERIFY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cli, "VERIFY_ATTEMPTS", 2)


def answer(monkeypatch, reply):
    def fake_input(prompt=""):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(builtins, "input", fake_input)


def run(handler, *argv) -> int:
    return cli.main([str(arg) for arg in argv], handlers={VM_TYPE: handler})


def test_disable_with_yes_acts_and_exits_zero(resources, state_file, capsys):
    handler = FakeHandler({"app": RUNNING})

    code = run(handler, "disable", "--resources", resources, "--state-file", state_file, "--yes")

    assert code == cli.EXIT_OK
    assert handler.calls == [("disable", "app")]
    assert "weekend-saver disable: completed" in capsys.readouterr().out
    assert state_file.exists()


def test_dry_run_prints_plan_and_changes_nothing(resources, state_file, capsys):
    handler = FakeHandler({"app": RUNNING})

    code = run(
        handler, "disable", "--resources", resources, "--state-file", state_file, "--dry-run"
    )

    assert code == cli.EXIT_OK
    assert handler.calls == []
    assert "PLANNED" in capsys.readouterr().out


def test_failed_resource_gives_exit_code_one(resources, state_file):
    handler = FakeHandler({"app": RUNNING}, fail_action_on={"app"})

    code = run(handler, "disable", "--resources", resources, "--state-file", state_file, "--yes")

    assert code == cli.EXIT_FAILED


def test_missing_resource_file_gives_exit_code_two(tmp_path, state_file, capsys):
    handler = FakeHandler({})

    code = run(
        handler, "disable", "--resources", tmp_path / "nope.yaml", "--state-file", state_file
    )

    assert code == cli.EXIT_INVALID
    assert "not found" in capsys.readouterr().err


def test_enable_without_snapshot_gives_exit_code_two(resources, state_file, capsys):
    handler = FakeHandler({"app": STOPPED})

    code = run(handler, "enable", "--resources", resources, "--state-file", state_file, "--yes")

    assert code == cli.EXIT_INVALID
    assert handler.calls == []
    assert "--ignore-prior-state" in capsys.readouterr().out


def test_enable_with_ignore_prior_state_starts_stopped_resources(resources, state_file):
    handler = FakeHandler({"app": STOPPED})

    code = run(
        handler,
        "enable",
        "--resources",
        resources,
        "--state-file",
        state_file,
        "--yes",
        "--ignore-prior-state",
    )

    assert code == cli.EXIT_OK
    assert handler.calls == [("enable", "app")]


@pytest.mark.parametrize(
    ("reply", "expected_calls"), [("y", [("disable", "app")]), ("", []), ("no", [])]
)
def test_interactive_approval_only_proceeds_on_yes(
    resources, state_file, monkeypatch, capsys, reply, expected_calls
):
    answer(monkeypatch, reply)
    handler = FakeHandler({"app": RUNNING})

    code = run(handler, "disable", "--resources", resources, "--state-file", state_file)

    assert code == cli.EXIT_OK
    assert handler.calls == expected_calls
    assert "Plan: disable 1 of 1" in capsys.readouterr().out


def test_unattended_run_without_yes_or_checkpoints_is_an_error(
    resources, state_file, monkeypatch, capsys
):
    answer(monkeypatch, EOFError())
    handler = FakeHandler({"app": RUNNING})

    code = run(handler, "disable", "--resources", resources, "--state-file", state_file)

    assert code == cli.EXIT_INVALID
    assert handler.calls == []
    assert "--yes" in capsys.readouterr().err


def test_unattended_run_with_checkpoints_pauses_then_resumes_on_approval(
    resources, state_file, tmp_path, monkeypatch, capsys
):
    answer(monkeypatch, EOFError())
    handler = FakeHandler({"app": RUNNING})
    checkpoints = tmp_path / "checkpoints"
    common = ("--state-file", state_file, "--checkpoint-dir", checkpoints)

    paused = run(handler, "disable", "--resources", resources, *common)
    assert paused == cli.EXIT_AWAITING_APPROVAL
    assert handler.calls == []
    assert "resume" in capsys.readouterr().err

    resumed = run(handler, "resume", "--approve", *common)
    assert resumed == cli.EXIT_OK
    assert handler.calls == [("disable", "app")]

    nothing_left = run(handler, "resume", "--approve", *common)
    assert nothing_left == cli.EXIT_INVALID


def test_resume_with_reject_changes_nothing(resources, state_file, tmp_path, monkeypatch, capsys):
    answer(monkeypatch, EOFError())
    handler = FakeHandler({"app": RUNNING})
    common = ("--state-file", state_file, "--checkpoint-dir", tmp_path / "checkpoints")
    run(handler, "disable", "--resources", resources, *common)

    code = run(handler, "resume", "--reject", *common)

    assert code == cli.EXIT_OK
    assert handler.calls == []
    assert "rejected" in capsys.readouterr().out


def test_non_https_webhook_is_rejected_before_anything_runs(
    resources, state_file, monkeypatch, capsys
):
    monkeypatch.setenv(cli.WEBHOOK_ENV_VAR, "http://example.com/hook")
    handler = FakeHandler({"app": RUNNING})

    code = run(handler, "disable", "--resources", resources, "--state-file", state_file, "--yes")

    assert code == cli.EXIT_INVALID
    assert handler.calls == []
    assert "https" in capsys.readouterr().err


def test_unexpected_error_gives_exit_code_one(resources, state_file, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "build_workflow", explode)

    code = run(
        FakeHandler({}), "disable", "--resources", resources, "--state-file", state_file, "--yes"
    )

    assert code == cli.EXIT_FAILED


def fake_credential(signed_in: bool, login_error: Exception | None = None):
    class FakeCredential:
        built_with: ClassVar[list[dict]] = []

        def __init__(self, record_path, **options) -> None:
            self.record_path = record_path
            self.built_with.append(options)

        def status(self) -> SignInStatus:
            return SignInStatus(signed_in, "me@example.com" if signed_in else "")

        def login(self) -> SignInStatus:
            if login_error:
                raise login_error
            return self.status()

    return FakeCredential


def test_real_run_uses_the_signed_in_account(resources, state_file, monkeypatch):
    handler = FakeHandler({"app": RUNNING})
    built_with = []

    def fake_build(credential):
        built_with.append(type(credential).__name__)
        return {VM_TYPE: handler}

    monkeypatch.setattr(cli, "SignedInCredential", fake_credential(signed_in=True))
    monkeypatch.setattr(cli, "build_azure_handlers", fake_build)

    code = cli.main(
        ["disable", "--resources", str(resources), "--state-file", str(state_file), "--yes"]
    )

    assert code == cli.EXIT_OK
    assert built_with == ["FakeCredential"]
    assert handler.calls == [("disable", "app")]


def test_real_run_stops_early_when_nobody_is_signed_in(resources, state_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SignedInCredential", fake_credential(signed_in=False))

    code = cli.main(
        ["disable", "--resources", str(resources), "--state-file", str(state_file), "--yes"]
    )

    assert code == cli.EXIT_INVALID
    assert "az login" in capsys.readouterr().err


def test_login_reports_the_signed_in_account(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SignedInCredential", fake_credential(signed_in=True))

    code = cli.main(["login", "--data-dir", str(tmp_path)])

    assert code == cli.EXIT_OK
    assert "Signed in as me@example.com" in capsys.readouterr().out


def test_login_failure_is_an_error_without_internal_detail(tmp_path, monkeypatch, capsys):
    broken = fake_credential(signed_in=False, login_error=RuntimeError("AADSTS secret detail"))
    monkeypatch.setattr(cli, "SignedInCredential", broken)

    code = cli.main(["login", "--data-dir", str(tmp_path)])

    error = capsys.readouterr().err
    assert code == cli.EXIT_INVALID
    assert "RuntimeError" in error
    assert "AADSTS" not in error


def test_dashboard_command_serves_the_demo_without_opening_a_browser(monkeypatch):
    served = []
    monkeypatch.setattr(
        cli, "serve_dashboard", lambda services, **options: served.append((services, options))
    )

    code = cli.main(["dashboard", "--demo", "--no-open", "--port", "9001"])

    ((services, options),) = served
    assert code == cli.EXIT_OK
    assert options == {"port": 9001, "open_browser": False}
    assert services.demo is True
    assert services.auth.status().account == "demo@example.com"


def test_devui_command_serves_only_the_demo_workflow_locked_down(monkeypatch, capsys):
    served = []
    monkeypatch.setattr(cli, "_load_devui", lambda: lambda **options: served.append(options))
    azure = []
    monkeypatch.setattr(cli, "build_azure_handlers", azure.append)

    code = cli.main(["devui", "--port", "9002"])

    (options,) = served
    assert code == cli.EXIT_OK
    assert options["auth_enabled"] is True
    assert options["mode"] == "user"
    assert options["auth_token"] in capsys.readouterr().out
    assert options["port"] == 9002
    assert next(iter(options["entities"][0].executors)) == "load_targets"
    assert azure == []


def test_command_line_runs_also_accept_non_interactive_identities(
    resources, state_file, monkeypatch
):
    credential_class = fake_credential(signed_in=True)
    monkeypatch.setattr(cli, "SignedInCredential", credential_class)
    monkeypatch.setattr(
        cli, "build_azure_handlers", lambda _: {VM_TYPE: FakeHandler({"app": RUNNING})}
    )

    cli.main(["disable", "--resources", str(resources), "--state-file", str(state_file), "--yes"])

    assert credential_class.built_with[-1]["cli_factory"] is cli.DefaultAzureCredential


def test_state_defaults_to_the_per_user_folder(resources, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_data_dir", lambda: tmp_path / "per-user")
    handler = FakeHandler({"app": RUNNING})

    code = run(handler, "disable", "--resources", resources, "--yes")

    assert code == cli.EXIT_OK
    assert (tmp_path / "per-user" / "state.json").exists()


def test_devui_command_explains_how_to_install_it_when_missing(tmp_path, monkeypatch, capsys):
    def missing():
        raise cli.ConfigError("DevUI is not installed. Install it with `uv sync --extra devui`.")

    monkeypatch.setattr(cli, "_load_devui", missing)

    code = cli.main(["devui"])

    assert code == cli.EXIT_INVALID
    assert "uv sync --extra devui" in capsys.readouterr().err
