import builtins

import pytest

from tests.helpers import VM_TYPE, FakeHandler, vm_id
from weekend_saver import cli
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


def test_real_run_builds_azure_handlers_from_default_credential(resources, state_file, monkeypatch):
    handler = FakeHandler({"app": RUNNING})
    used = []

    class FakeCredential:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            used.append("closed")
            return False

    def fake_build(credential):
        used.append(type(credential).__name__)
        return {VM_TYPE: handler}

    monkeypatch.setattr(cli, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(cli, "build_azure_handlers", fake_build)

    code = cli.main(
        ["disable", "--resources", str(resources), "--state-file", str(state_file), "--yes"]
    )

    assert code == cli.EXIT_OK
    assert used == ["FakeCredential", "closed"]
    assert handler.calls == [("disable", "app")]
