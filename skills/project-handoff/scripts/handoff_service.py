"""Orchestrate publication and continuation of project handoffs."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat
import tempfile
import time


_REQUIRED_SECTIONS = (
    "Completed Work",
    "Agreed Rules and Decisions",
    "Verification Status",
    "Next Step",
)
_ERROR_SUMMARY_LIMIT = 512


class HandoffValidationError(ValueError):
    """A validation error whose fixed message is safe for CLI output."""


class PublicationRollbackError(OSError):
    """A failed rollback means publication containment is uncertain."""


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
        return self._store_call(
            self.store.arm,
            pending_id,
            session_id,
            timeout_seconds,
        )

    def respond(self, session_id):
        return self.store.respond(session_id)

    def confirm(self, pending_id):
        record = self._store_call(self.store.claim_confirm, pending_id)
        if record is None:
            return None
        return self._transfer_claimed(record)

    def cancel(self, pending_id):
        return self._store_call(self.store.cancel, pending_id)

    def wait_and_expire(self, pending_id):
        while True:
            record = self._pending_record(pending_id)
            if record["state"] != "armed":
                return None
            remaining = (
                float(record["deadline_at"]) - float(self.store.now())
            )
            if remaining > 0:
                self.sleeper(remaining)
                continue
            claimed = self.store.claim_expired(pending_id)
            if claimed is None:
                continue
            return self._transfer_claimed(claimed)

    def status(self, session_id):
        return self.store.get_session_status(session_id)

    def _transfer_claimed(self, record):
        handoff_path = None
        try:
            target = self._project_target(record)
            handoff_path = self._publish_with_fallback(
                str(record["pending_id"]),
                Path(str(record["cwd"])),
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
        try:
            with self.store._locked(pending_id):
                return self.store._read_record(
                    self.store._pending_path(pending_id)
                )
        except ValueError as error:
            self._raise_safe_store_error(error)

    @staticmethod
    def _store_call(operation, *args):
        try:
            return operation(*args)
        except ValueError as error:
            HandoffService._raise_safe_store_error(error)

    @staticmethod
    def _raise_safe_store_error(error):
        message = str(error)
        safe_messages = {
            "invalid pending id",
            "timeout_seconds must be exactly 300",
        }
        if message in safe_messages:
            raise HandoffValidationError(message) from None
        raise error

    @staticmethod
    def _validate_handoff(handoff_text):
        headings = HandoffService._top_level_headings(handoff_text)
        missing = [
            section
            for section in _REQUIRED_SECTIONS
            if section not in headings
        ]
        if missing:
            raise HandoffValidationError(
                "handoff is missing required sections"
            )

    @staticmethod
    def _top_level_headings(handoff_text):
        headings = set()
        fence_character = None
        fence_length = 0
        for line in handoff_text.splitlines():
            leading_spaces = len(line) - len(line.lstrip(" "))
            content = line[leading_spaces:] if leading_spaces <= 3 else ""
            if fence_character is not None:
                run_length = len(content) - len(
                    content.lstrip(fence_character)
                )
                if (
                    run_length >= fence_length
                    and not content[run_length:].strip()
                ):
                    fence_character = None
                    fence_length = 0
                continue
            if line.startswith("\t") or leading_spaces >= 4:
                continue
            if content.startswith(("```", "~~~")):
                fence_character = content[0]
                fence_length = len(content) - len(
                    content.lstrip(fence_character)
                )
                continue
            if content.startswith("# "):
                headings.add(content[2:].rstrip())
        return headings

    @staticmethod
    def _project_target(record):
        return HandoffService._resolve_target(
            Path(str(record["cwd"])),
            str(record["target_path"]),
        )

    @staticmethod
    def _resolve_target(cwd, target_path):
        cwd = Path(os.path.abspath(cwd))
        configured = Path(target_path)
        target = Path(
            os.path.abspath(
                configured if configured.is_absolute() else cwd / configured
            )
        )
        if target == cwd or cwd not in target.parents:
            raise HandoffValidationError("target path must stay beneath cwd")
        return target

    def _publish_with_fallback(self, pending_id, cwd, target, handoff_text):
        try:
            self._publish_project(cwd, target, handoff_text)
            return target
        except PublicationRollbackError:
            raise
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
        self._publish_path(target, handoff_text)

    @staticmethod
    def _publish_project(cwd, target, handoff_text):
        """Publish without following target components outside ``cwd``.

        The replacement is anchored to a verified parent descriptor. A rename
        after the final identity check can make the prompt path stale, but it
        cannot redirect the already-completed write through a symlink.
        Rollback compares the published inode before unlinking; POSIX stdlib
        cannot make that comparison and unlink one indivisible operation.
        """
        relative = target.relative_to(cwd)
        if not relative.parts or relative.name in {"", ".", ".."}:
            raise HandoffValidationError("target path must stay beneath cwd")
        parent_parts = relative.parts[:-1]
        parent_fd = HandoffService._open_project_parent(
            cwd,
            parent_parts,
            create=True,
        )
        temporary_name = None
        try:
            HandoffService._reject_symlink_entry(parent_fd, relative.name)
            temporary_name, temporary_fd = HandoffService._create_temp_at(
                parent_fd
            )
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                handle.write(handoff_text)
                handle.flush()
                os.fsync(handle.fileno())
                published_identity = HandoffService._file_identity(
                    os.fstat(handle.fileno())
                )
            HandoffService._verify_project_parent(
                cwd,
                parent_parts,
                parent_fd,
            )
            os.replace(
                temporary_name,
                relative.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = None
            try:
                os.fsync(parent_fd)
                HandoffService._verify_project_parent(
                    cwd,
                    parent_parts,
                    parent_fd,
                )
            except OSError:
                try:
                    HandoffService._rollback_published_file(
                        parent_fd,
                        relative.name,
                        published_identity,
                    )
                except OSError as rollback_error:
                    raise PublicationRollbackError(
                        "unable to roll back unsafe project publication"
                    ) from rollback_error
                raise
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    @staticmethod
    def _verify_project_parent(cwd, parent_parts, parent_fd):
        verification_fd = HandoffService._open_project_parent(
            cwd,
            parent_parts,
            create=False,
        )
        try:
            if HandoffService._file_identity(
                os.fstat(parent_fd)
            ) != HandoffService._file_identity(os.fstat(verification_fd)):
                raise OSError("project target parent changed during publish")
        finally:
            os.close(verification_fd)

    @staticmethod
    def _rollback_published_file(parent_fd, name, published_identity):
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise OSError("published project target disappeared") from error
        if HandoffService._file_identity(current) != published_identity:
            raise OSError("published project target changed before rollback")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)

    @staticmethod
    def _file_identity(metadata):
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _open_project_parent(cwd, parent_parts, create):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        try:
            for component in cwd.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            for component in parent_parts:
                if component in {"", ".", ".."}:
                    raise OSError("unsafe project target component")
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _reject_symlink_entry(parent_fd, name):
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("project target is a symlink")

    @staticmethod
    def _create_temp_at(parent_fd):
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
        )
        for _attempt in range(100):
            name = f".handoff-{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            try:
                os.fchmod(descriptor, 0o600)
            except BaseException:
                try:
                    os.close(descriptor)
                finally:
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                raise
            return name, descriptor
        raise OSError("unable to create handoff temporary file")

    @staticmethod
    def _publish_path(target, handoff_text):
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
