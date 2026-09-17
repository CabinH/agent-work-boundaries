"""Command-line controller for project handoffs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from app_server_client import AppServerClient
from handoff_service import HandoffService, HandoffValidationError
from state_store import StateStore


class _ParserError(Exception):
    pass


class _HelpRequested(Exception):
    def __init__(self, help_text):
        super().__init__()
        self.help_text = help_text


class _CliValidationError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._captured_messages = []

    def _print_message(self, message, file=None):
        if message:
            self._captured_messages.append(message)

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message)
        if status == 0:
            raise _HelpRequested("".join(self._captured_messages))
        raise _ParserError

    def error(self, message):
        raise _ParserError


def _parser():
    parser = _ArgumentParser(prog="handoffctl")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--cwd", required=True)
    prepare.add_argument("--from-file", required=True)
    prepare.add_argument("--target", required=True)

    arm = commands.add_parser("arm")
    arm.add_argument("--pending-id", required=True)
    arm.add_argument("--session-id", required=True)
    arm.add_argument("--timeout-seconds", required=True, type=int)

    respond = commands.add_parser("respond")
    respond.add_argument("--session-id", required=True)

    for name in ("confirm", "cancel", "wait"):
        command = commands.add_parser(name)
        command.add_argument("--pending-id", required=True)

    status = commands.add_parser("status")
    status.add_argument("--session-id", required=True)
    return parser


def _build_service():
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    state_root = codex_home / "state" / "project-handoff"
    return HandoffService(
        store=StateStore(state_root, now=time.time),
        app_server_client=AppServerClient(),
        private_handoff_dir=state_root / "handoffs",
    )


def _read_draft(path, stdin):
    if path == "-":
        return stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        raise _CliValidationError("unable to read handoff draft") from None


def _run(args, service, stdin):
    if args.command == "prepare":
        pending_id = service.prepare(
            args.cwd,
            _read_draft(args.from_file, stdin),
            args.target,
        )
        return {"pending_id": pending_id}
    if args.command == "arm":
        return service.arm(
            args.pending_id,
            args.session_id,
            args.timeout_seconds,
        )
    if args.command == "respond":
        return service.respond(args.session_id)
    if args.command == "confirm":
        return service.confirm(args.pending_id)
    if args.command == "cancel":
        return service.cancel(args.pending_id)
    if args.command == "wait":
        return service.wait_and_expire(args.pending_id)
    return service.status(args.session_id)


def _write_json(stream, payload):
    json.dump(payload, stream, sort_keys=True)
    stream.write("\n")


def main(argv=None, stdin=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
        result = _run(args, _build_service(), stdin)
        _write_json(stdout, result if result is not None else {"result": None})
        return 0
    except _HelpRequested as signal:
        _write_json(stdout, {"help": signal.help_text})
        return 0
    except _ParserError:
        _write_json(stdout, {"error": "invalid arguments"})
        return 2
    except (HandoffValidationError, _CliValidationError) as error:
        _write_json(stdout, {"error": str(error)})
        return 2
    except Exception:
        _write_json(stdout, {"error": "command failed"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
