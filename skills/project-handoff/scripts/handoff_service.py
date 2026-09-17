"""Orchestrate publication and continuation of project handoffs."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time


_REQUIRED_SECTIONS = (
    "Completed Work",
    "Agreed Rules and Decisions",
    "Verification Status",
    "Next Step",
)
_ERROR_SUMMARY_LIMIT = 512


class HandoffService:
    def __init__(
        self,
        store,
        app_server_client,
        private_handoff_dir,
        sleeper=time.sleep,
    ):
        self.store = store
        self.app_server_client = app_server_client
        self.private_handoff_dir = Path(private_handoff_dir)
        self.sleeper = sleeper

    def prepare(self, cwd, handoff_text, target_path):
        self._validate_handoff(handoff_text)
        cwd = Path(cwd).resolve()
        self._resolve_target(cwd, target_path)
        record = self.store.prepare(str(cwd), handoff_text, str(target_path))
        return str(record["pending_id"])

    def arm(self, pending_id, session_id, timeout_seconds=300):
        return self.store.arm(pending_id, session_id, timeout_seconds)

    def respond(self, session_id):
        return self.store.respond(session_id)

    def confirm(self, pending_id):
        record = self.store.claim_confirm(pending_id)
        if record is None:
            return None
        return self._transfer_claimed(record)

    def cancel(self, pending_id):
        return self.store.cancel(pending_id)

    def wait_and_expire(self, pending_id):
        record = self._pending_record(pending_id)
        if record["state"] != "armed":
            return None
        remaining = max(
            0.0,
            float(record["deadline_at"]) - float(self.store.now()),
        )
        if remaining:
            self.sleeper(remaining)
        claimed = self.store.claim_expired(pending_id)
        if claimed is None:
            return None
        return self._transfer_claimed(claimed)

    def status(self, session_id):
        return self.store.get_session_status(session_id)

    def _transfer_claimed(self, record):
        handoff_path = None
        try:
            target = self._project_target(record)
            handoff_path = self._publish_with_fallback(
                str(record["pending_id"]),
                target,
                str(record["handoff_text"]),
            )
            prompt = self._resume_prompt(record, handoff_path)
            launched = self.app_server_client.launch(str(record["cwd"]), prompt)
            return self.store.mark_transferred(
                str(record["pending_id"]),
                launched.thread_id,
            )
        except Exception as error:
            if handoff_path is None:
                recovery_path = self._private_target(str(record["pending_id"]))
                try:
                    self._publish_private(recovery_path, str(record["handoff_text"]))
                except OSError:
                    pass
            else:
                recovery_path = handoff_path
            recovery_prompt = self._format_resume_prompt(record, recovery_path)
            self.store.mark_failed(
                str(record["pending_id"]),
                self._error_summary(error),
                recovery_prompt,
            )
            raise

    def _pending_record(self, pending_id):
        with self.store._locked(pending_id):
            return self.store._read_record(self.store._pending_path(pending_id))

    @staticmethod
    def _validate_handoff(handoff_text):
        headings = {line.strip() for line in handoff_text.splitlines()}
        missing = [
            section
            for section in _REQUIRED_SECTIONS
            if f"# {section}" not in headings
        ]
        if missing:
            raise ValueError("handoff is missing required sections")

    @staticmethod
    def _project_target(record):
        return HandoffService._resolve_target(
            Path(str(record["cwd"])).resolve(),
            str(record["target_path"]),
        )

    @staticmethod
    def _resolve_target(cwd, target_path):
        configured = Path(target_path)
        target = (
            (cwd / configured).resolve()
            if not configured.is_absolute()
            else configured.resolve()
        )
        if target == cwd or cwd not in target.parents:
            raise ValueError("target path must stay beneath cwd")
        return target

    def _publish_with_fallback(self, pending_id, target, handoff_text):
        try:
            self._publish(target, handoff_text)
            return target
        except OSError:
            fallback = self._private_target(pending_id)
            self._publish_private(fallback, handoff_text)
            return fallback

    def _private_target(self, pending_id):
        return self.private_handoff_dir / f"{pending_id}.md"

    def _publish_private(self, target, handoff_text):
        self.private_handoff_dir.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )
        self.private_handoff_dir.chmod(0o700)
        self._publish(target, handoff_text)

    @staticmethod
    def _publish(target, handoff_text):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(handoff_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _resume_prompt(record, handoff_path):
        return HandoffService._format_resume_prompt(record, handoff_path)

    @staticmethod
    def _format_resume_prompt(record, handoff_path):
        return (
            "This thread continues work handed off from "
            f"{record['session_id']}.\n"
            f"Read project instructions and {handoff_path}. Verify durable "
            "source-of-truth files before trusting the summary.\n"
            "Continue from the single “Next Step” in the handoff. Do not redo "
            "completed work. Report any contradiction before changing files."
        )

    @staticmethod
    def _error_summary(error):
        summary = f"{type(error).__name__}: {error}"
        return summary[:_ERROR_SUMMARY_LIMIT]
