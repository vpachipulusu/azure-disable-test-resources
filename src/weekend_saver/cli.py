"""Command-line entry point.

weekend-saver dashboard [--demo] [--port N] [--no-open]
weekend-saver login
weekend-saver disable --resources resources.yaml [--dry-run] [--yes]
weekend-saver enable  --resources resources.yaml [--dry-run] [--yes] [--ignore-prior-state]
weekend-saver resume  --checkpoint-dir DIR (--approve | --reject)
weekend-saver devui   [--demo]
"""

import argparse
import asyncio
import logging
import os
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from agent_framework import FileCheckpointStorage
from azure.identity import DefaultAzureCredential

from weekend_saver.auth import AUTH_RECORD_FILE, SignedInCredential
from weekend_saver.azure_handlers import build_azure_handlers
from weekend_saver.config import load_resource_specs
from weekend_saver.dashboard.demo import demo_handlers
from weekend_saver.dashboard.server import build_services
from weekend_saver.dashboard.server import serve as serve_dashboard
from weekend_saver.errors import ApprovalUnavailableError, ConfigError
from weekend_saver.executors import (
    DEFAULT_VERIFY_ATTEMPTS,
    DEFAULT_VERIFY_INTERVAL_SECONDS,
    describe_error,
)
from weekend_saver.files import default_data_dir, ensure_private_dir
from weekend_saver.handlers import ResourceHandler
from weekend_saver.models import (
    CHECKPOINT_TYPES,
    Action,
    ApprovalRequest,
    RunReport,
    RunRequest,
    RunStatus,
)
from weekend_saver.notifier import Notifier, WebhookNotifier
from weekend_saver.report import format_plan, format_report
from weekend_saver.runner import resume_with_decision, run_to_completion
from weekend_saver.state_store import JsonFileStateStore
from weekend_saver.workflow import WORKFLOW_NAME, build_workflow

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_AWAITING_APPROVAL = 3

DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_DEVUI_PORT = 8080
WEBHOOK_ENV_VAR = "WEEKEND_SAVER_WEBHOOK_URL"
RESUME = "resume"
DASHBOARD = "dashboard"
LOGIN = "login"
DEVUI = "devui"

VERIFY_ATTEMPTS = DEFAULT_VERIFY_ATTEMPTS
VERIFY_INTERVAL_SECONDS = DEFAULT_VERIFY_INTERVAL_SECONDS


def main(
    argv: Sequence[str] | None = None,
    *,
    handlers: Mapping[str, ResourceHandler] | None = None,
) -> int:
    """Run the CLI and return the process exit code. `handlers` replaces Azure in tests."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        # Only this tool's logger: SDK debug output is noisy and full of request detail
        logging.getLogger("weekend_saver").setLevel(logging.DEBUG)
    try:
        if args.command in _LOCAL_COMMANDS:
            return _LOCAL_COMMANDS[args.command](args)
        report = asyncio.run(_dispatch(args, handlers))
    except ApprovalUnavailableError:
        return _approval_unavailable(args.checkpoint_dir)
    except ConfigError as exc:
        _emit(sys.stderr, f"error: {exc}")
        return EXIT_INVALID
    except Exception:
        logger.exception("weekend-saver failed unexpectedly")
        return EXIT_FAILED

    _emit(sys.stdout, format_report(report))
    return _exit_code(report)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-file",
        type=Path,
        help="where to remember which resources were disabled "
        "(default: state.json in your per-user weekend-saver folder)",
    )
    common.add_argument(
        "--checkpoint-dir",
        type=Path,
        help="keep workflow checkpoints here so a run awaiting approval can be resumed",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="log debug detail")

    run_options = argparse.ArgumentParser(add_help=False)
    run_options.add_argument(
        "--resources", type=Path, required=True, help="YAML file listing ARM resource IDs"
    )
    run_options.add_argument(
        "--dry-run", action="store_true", help="show what would change, then stop"
    )
    run_options.add_argument(
        "--yes", action="store_true", help="skip the approval prompt (for scheduled runs)"
    )

    parser = argparse.ArgumentParser(
        prog="weekend-saver",
        description="Disable Azure resources for the weekend and enable them again afterwards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        Action.DISABLE.value, parents=[common, run_options], help="stop the listed resources"
    )
    enable = commands.add_parser(
        Action.ENABLE.value,
        parents=[common, run_options],
        help="start the resources the last disable run stopped",
    )
    enable.add_argument(
        "--ignore-prior-state",
        action="store_true",
        help="start every listed resource that is stopped, whoever stopped it",
    )
    resume = commands.add_parser(
        RESUME, parents=[common], help="answer a run that paused for approval"
    )
    decision = resume.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", dest="decision", action="store_const", const=True)
    decision.add_argument("--reject", dest="decision", action="store_const", const=False)

    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument(
        "--data-dir",
        type=Path,
        help="folder for the selection, state, schedule, run history and sign-in record "
        "(default: your per-user weekend-saver folder)",
    )

    dashboard = commands.add_parser(
        DASHBOARD, parents=[verbose, data], help="open the local web dashboard"
    )
    dashboard.add_argument(
        "--demo", action="store_true", help="use made-up resources instead of signing in to Azure"
    )
    dashboard.add_argument(
        "--resources",
        type=Path,
        help="YAML file for the selection (default: resources.yaml in the data folder)",
    )
    dashboard.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    dashboard.add_argument(
        "--no-open", action="store_true", help="print the link instead of opening a browser"
    )
    commands.add_parser(LOGIN, parents=[verbose, data], help="sign in to Azure through a browser")
    devui = commands.add_parser(
        DEVUI,
        parents=[verbose],
        help="inspect the workflow in Agent Framework DevUI (made-up resources only)",
    )
    devui.add_argument("--port", type=int, default=DEFAULT_DEVUI_PORT)
    return parser


def _data_dir(args: argparse.Namespace) -> Path:
    return args.data_dir or default_data_dir()


def _run_dashboard(args: argparse.Namespace) -> int:
    services = build_services(
        resources_path=args.resources,
        data_dir=_data_dir(args),
        demo=args.demo,
        notifier=_notifier_from_environment(),
    )
    serve_dashboard(services, port=args.port, open_browser=not args.no_open)
    return EXIT_OK


def _run_login(args: argparse.Namespace) -> int:
    credential = SignedInCredential(ensure_private_dir(_data_dir(args)) / AUTH_RECORD_FILE)
    try:
        status = credential.login()
    except Exception as exc:
        logger.debug("Browser sign-in failed", exc_info=True)
        raise ConfigError(
            f"Sign-in did not complete ({describe_error(exc)}). Try `az login` instead."
        ) from exc
    if not status.signed_in:
        raise ConfigError("Sign-in finished but no Azure token could be obtained.")
    _emit(sys.stdout, f"Signed in as {status.account or 'unknown account'}.")
    return EXIT_OK


def _run_devui(args: argparse.Namespace) -> int:
    serve_devui = _load_devui()
    # DevUI is a development sample whose input form can ask for pre-approved runs on any
    # resource, so it only ever gets the made-up resources, never a real sign-in.
    state_dir = Path(tempfile.mkdtemp(prefix="weekend-saver-devui-"))
    workflow = build_workflow(demo_handlers(), JsonFileStateStore(state_dir / "state.json"))
    token = secrets.token_urlsafe(24)
    _emit(sys.stdout, f"DevUI with made-up resources. Sign-in token for its page: {token}")
    serve_devui(
        entities=[workflow],
        port=args.port,
        auto_open=True,
        mode="user",
        auth_enabled=True,
        auth_token=token,
    )
    return EXIT_OK


def _load_devui() -> Callable[..., Any]:
    try:
        from agent_framework.devui import serve
    except ImportError as exc:
        raise ConfigError(
            "DevUI is not installed. Install the optional extra with `uv sync --extra devui`."
        ) from exc
    return serve


_LOCAL_COMMANDS: Mapping[str, Callable[[argparse.Namespace], int]] = {
    DASHBOARD: _run_dashboard,
    LOGIN: _run_login,
    DEVUI: _run_devui,
}


async def _dispatch(
    args: argparse.Namespace, handlers: Mapping[str, ResourceHandler] | None
) -> RunReport:
    # Everything that can be rejected without Azure is checked before a credential exists
    notifier = _notifier_from_environment()
    request = _request_from(args)
    if args.command == RESUME and args.checkpoint_dir is None:
        raise ConfigError("resume needs --checkpoint-dir.")

    if handlers is not None:
        return await _execute(args, request, handlers, notifier)
    # The command line may run unattended, so besides `az login` it also accepts managed
    # identities, workload identity and service-principal environment variables.
    credential = SignedInCredential(
        default_data_dir() / AUTH_RECORD_FILE,
        cli_factory=DefaultAzureCredential,
        primary_name="default credential chain",
    )
    status = await asyncio.to_thread(credential.status)
    if not status.signed_in:
        raise ConfigError("Not signed in to Azure. Run `az login` or `weekend-saver login`.")
    return await _execute(args, request, build_azure_handlers(credential), notifier)


async def _execute(
    args: argparse.Namespace,
    request: RunRequest | None,
    handlers: Mapping[str, ResourceHandler],
    notifier: Notifier | None,
) -> RunReport:
    checkpoints = _checkpoint_storage(args.checkpoint_dir)
    workflow = build_workflow(
        handlers,
        JsonFileStateStore(args.state_file or default_data_dir() / "state.json"),
        notifier=notifier,
        checkpoint_storage=checkpoints,
        verify_attempts=VERIFY_ATTEMPTS,
        verify_interval_seconds=VERIFY_INTERVAL_SECONDS,
    )
    if request is None:
        return await resume_with_decision(
            workflow, checkpoints, WORKFLOW_NAME, approved=args.decision
        )
    return await run_to_completion(workflow, request, _ask_on_console)


def _request_from(args: argparse.Namespace) -> RunRequest | None:
    if args.command == RESUME:
        return None
    return RunRequest(
        action=Action(args.command),
        resources=load_resource_specs(args.resources),
        dry_run=args.dry_run,
        auto_approve=args.yes,
        ignore_prior_state=getattr(args, "ignore_prior_state", False),
    )


def _notifier_from_environment() -> Notifier | None:
    url = os.environ.get(WEBHOOK_ENV_VAR)
    return WebhookNotifier(url) if url else None


def _checkpoint_storage(directory: Path | None) -> FileCheckpointStorage | None:
    if directory is None:
        return None
    return FileCheckpointStorage(directory, allowed_checkpoint_types=CHECKPOINT_TYPES)


async def _ask_on_console(request: ApprovalRequest) -> bool:
    _emit(sys.stdout, format_plan(request.plan))
    try:
        answer = await asyncio.to_thread(input, "Proceed? [y/N] ")
    except EOFError as exc:
        raise ApprovalUnavailableError("stdin is not interactive") from exc
    return answer.strip().lower() in {"y", "yes"}


def _approval_unavailable(checkpoint_dir: Path | None) -> int:
    if checkpoint_dir is None:
        _emit(
            sys.stderr,
            "error: this plan needs approval but there is no interactive terminal. "
            "Pass --yes for unattended runs, or --checkpoint-dir to pause and approve later.",
        )
        return EXIT_INVALID
    _emit(
        sys.stderr,
        "Paused awaiting approval; nothing has changed. Continue with: "
        f"weekend-saver resume --checkpoint-dir {checkpoint_dir} --approve (or --reject)",
    )
    return EXIT_AWAITING_APPROVAL


def _exit_code(report: RunReport) -> int:
    if report.status is RunStatus.INVALID:
        return EXIT_INVALID
    return EXIT_FAILED if report.failed else EXIT_OK


def _emit(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
