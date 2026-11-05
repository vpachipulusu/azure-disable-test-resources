"""Command-line entry point.

weekend-saver disable --resources resources.yaml [--dry-run] [--yes]
weekend-saver enable  --resources resources.yaml [--dry-run] [--yes] [--ignore-prior-state]
weekend-saver resume  --checkpoint-dir DIR (--approve | --reject)
"""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from agent_framework import FileCheckpointStorage
from azure.identity import DefaultAzureCredential

from weekend_saver.azure_handlers import build_azure_handlers
from weekend_saver.config import load_resource_specs
from weekend_saver.errors import ApprovalUnavailableError, ConfigError
from weekend_saver.executors import DEFAULT_VERIFY_ATTEMPTS, DEFAULT_VERIFY_INTERVAL_SECONDS
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

DEFAULT_STATE_FILE = Path(".weekend-saver") / "state.json"
WEBHOOK_ENV_VAR = "WEEKEND_SAVER_WEBHOOK_URL"
RESUME = "resume"

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
        default=DEFAULT_STATE_FILE,
        help="where to remember which resources were disabled (default: %(default)s)",
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
    return parser


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
    with DefaultAzureCredential() as credential:
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
        JsonFileStateStore(args.state_file),
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
