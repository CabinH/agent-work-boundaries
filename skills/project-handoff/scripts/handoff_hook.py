"""Translate Codex lifecycle Hook events into project-handoff actions."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


HANDOFFCTL_PATH = Path(__file__).with_name("handoffctl.py").resolve()

_PENDING_MARKER = re.compile(
    r"<!-- project-handoff:pending="
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r" -->"
)


def handle_event(
    payload,
    service,
    spawn_worker,
) -> dict[str, object] | None:
    """Handle one validated-enough Hook payload without owning persistence."""
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("hook_event_name")
    if event_name == "Stop":
        return _handle_stop(payload, service, spawn_worker)
    if event_name == "UserPromptSubmit":
        return _handle_user_prompt(payload, service)
    if event_name == "PostCompact":
        return _handle_post_compact(payload, service)
    if event_name == "SessionStart":
        return _handle_session_start(payload, service)
    return None


def _handle_stop(payload, service, spawn_worker):
    session_id = payload.get("session_id")
    message = payload.get("last_assistant_message")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(message, str):
        return None
    matches = _PENDING_MARKER.findall(message)
    if len(matches) != 1:
        return None
    pending_id = matches[0]
    armed = service.arm(pending_id, session_id, 300)
    if armed is None:
        return None
    try:
        spawn_worker(
            [
                sys.executable,
                str(HANDOFFCTL_PATH),
                "wait",
                "--pending-id",
                pending_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        disabled = None
        try:
            disabled = service.respond(session_id)
        except Exception:
            pass
        if disabled is None:
            try:
                disabled = service.cancel(pending_id)
            except Exception:
                pass
        if isinstance(disabled, dict) and disabled.get("state") == "responded":
            confirm_command = _control_command(
                "confirm",
                "--pending-id",
                pending_id,
            )
            cancel_command = _control_command(
                "cancel",
                "--pending-id",
                pending_id,
            )
            message = (
                "The handoff timer did not start, so automatic transfer is "
                "disabled. To transfer explicitly, run "
                f"{confirm_command}; to remain here, run {cancel_command}."
            )
        else:
            if (
                isinstance(disabled, dict)
                and disabled.get("state") == "cancelled"
            ):
                status_command = _control_command(
                    "status",
                    "--session-id",
                    session_id,
                )
                message = (
                    "The handoff timer did not start and automatic transfer "
                    f"was cancelled. Run {status_command} for the current "
                    "state and recovery details."
                )
            else:
                confirm_command = _control_command(
                    "confirm",
                    "--pending-id",
                    pending_id,
                )
                cancel_command = _control_command(
                    "cancel",
                    "--pending-id",
                    pending_id,
                )
                message = (
                    "The automatic timer is not running, and the handoff "
                    "state could not be changed automatically. To transfer, "
                    f"run {confirm_command}; to disarm, run {cancel_command}."
                )
        return {
            "systemMessage": message
        }
    return None


def _handle_user_prompt(payload, service):
    session_id = payload.get("session_id")
    prompt = payload.get("prompt")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(prompt, str):
        return None

    status = service.status(session_id)
    terminal_output = _terminal_prompt_output(status, session_id)
    if terminal_output is not None:
        return terminal_output

    responded = service.respond(session_id)
    if responded is not None:
        pending_id = str(responded["pending_id"])
        confirm_command = _control_command(
            "confirm",
            "--pending-id",
            pending_id,
        )
        cancel_command = _control_command(
            "cancel",
            "--pending-id",
            pending_id,
        )
        return _additional_context(
            "UserPromptSubmit",
            (
                "A pending handoff countdown was cancelled by this prompt. "
                "If the user confirms, run "
                f"{confirm_command}; "
                "if the user rejects, run "
                f"{cancel_command}; "
                "otherwise continue here and do not transfer automatically."
            ),
        )

    status = service.status(session_id)
    terminal_output = _terminal_prompt_output(status, session_id)
    if terminal_output is not None:
        return terminal_output
    if isinstance(status, dict) and status.get("state") == "armed":
        return _in_progress_block(session_id)
    return None


def _terminal_prompt_output(status, session_id):
    if not isinstance(status, dict):
        return None
    state = status.get("state")
    if state in {"transferred", "superseded"}:
        destination = status.get("new_thread_id")
        if not isinstance(destination, str) or not destination:
            return _in_progress_block(session_id)
        return {
            "decision": "block",
            "reason": (
                f"This conversation was handed off to thread {destination}; "
                "open that thread instead of continuing duplicate work."
            ),
        }
    if state == "failed":
        context = _failed_recovery_context(status)
        if context is None:
            return None
        return _additional_context(
            "UserPromptSubmit",
            context,
        )
    if state in {"transferring", "expired"}:
        return _in_progress_block(session_id)
    return None


def _in_progress_block(session_id):
    status_command = _control_command(
        "status",
        "--session-id",
        session_id,
    )
    return {
        "decision": "block",
        "reason": (
            "This conversation has a handoff in progress; do not continue "
            "duplicate work here. Run "
            f"{status_command} to find the "
            "destination or recovery instructions."
        ),
    }


def _handle_post_compact(payload, service):
    session_id = payload.get("session_id")
    trigger = payload.get("trigger")
    if not isinstance(session_id, str) or not session_id:
        return None
    if trigger not in {"manual", "auto"}:
        return None
    count = service.store.record_compaction(session_id, trigger)
    return {
        "systemMessage": (
            f"Recorded project handoff compaction {count} ({trigger})."
        )
    }


def _handle_session_start(payload, service):
    session_id = payload.get("session_id")
    source = payload.get("source")
    if not isinstance(session_id, str) or not session_id:
        return None
    if source not in {"startup", "resume", "clear", "compact"}:
        return None

    status = service.status(session_id)
    contexts = []
    recovery = _failed_recovery_context(status)
    if recovery is not None:
        contexts.append(recovery)

    if source == "compact" and isinstance(status, dict):
        count = status.get("compaction_count", 0)
        if isinstance(count, int) and not isinstance(count, bool):
            if count == 2:
                contexts.append(
                    "This conversation has been compacted 2 times. At the "
                    "next stable boundary, request a project handoff. Do not "
                    "interrupt a non-interruptible step."
                )
            elif count >= 3:
                contexts.append(
                    f"This conversation has been compacted {count} times. "
                    "After the current non-interruptible step, strongly "
                    "request a project handoff before starting another major "
                    "work segment in this thread."
                )

    if not contexts:
        return None
    context = " ".join(contexts)
    if len(context.split()) >= 120 and recovery is not None:
        contexts[0] = _failed_recovery_fallback(status)
        context = " ".join(contexts)
    return _additional_context("SessionStart", context)


def _failed_recovery_context(status):
    if not isinstance(status, dict) or status.get("state") != "failed":
        return None
    recovery = status.get("recovery_prompt")
    if not isinstance(recovery, str) or not recovery:
        return None
    context = f"The previous handoff transfer failed. Recover with: {recovery}"
    if len(context.split()) < 120:
        return context
    return _failed_recovery_fallback(status)


def _failed_recovery_fallback(status):
    session_id = status.get("session_id")
    if isinstance(session_id, str) and session_id:
        status_command = _control_command(
            "status",
            "--session-id",
            session_id,
        )
        context = (
            "The previous handoff transfer failed. Run "
            f"{status_command} and use its full "
            "recovery_prompt in /new."
        )
        if len(context.split()) < 80:
            return context
        return (
            "The previous handoff transfer failed, but its status command is "
            "too long to embed safely. Open a new conversation and recover "
            "from the saved project-handoff state rather than continuing here."
        )
    return (
        "The previous handoff transfer failed, but its session identifier is "
        "unavailable. Open a new conversation and recover from the saved "
        "project-handoff state rather than continuing here."
    )


def _control_command(subcommand, identifier_option, identifier):
    return shlex.join(
        [
            sys.executable,
            str(HANDOFFCTL_PATH),
            subcommand,
            identifier_option,
            identifier,
        ]
    )


def _additional_context(event_name, context):
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }


def _build_service():
    from handoffctl import _build_service as build_controller_service

    return build_controller_service()


def main(
    stdin=None,
    stdout=None,
    stderr=None,
    service=None,
    spawn_worker=None,
):
    """Read one Hook event and write either one JSON object or nothing."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        service = _build_service() if service is None else service
        spawn_worker = (
            subprocess.Popen if spawn_worker is None else spawn_worker
        )
        payload = json.load(stdin)
        result = handle_event(payload, service, spawn_worker)
        if result is not None:
            json.dump(result, stdout, sort_keys=True)
            stdout.write("\n")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
