"""Atomic, process-safe persistence for project handoff state."""

from __future__ import annotations

from collections.abc import Callable
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


LEGAL_TRANSITIONS = {
    "draft": {"armed", "cancelled"},
    "armed": {"responded", "expired", "transferring", "cancelled"},
    "responded": {"transferring", "cancelled"},
    "expired": {"transferring"},
    "transferring": {"thread_starting", "failed"},
    "thread_starting": {"thread_created", "indeterminate"},
    "thread_created": {"turn_starting"},
    "turn_starting": {"transferred", "indeterminate"},
    "failed": {"transferring", "cancelled"},
    "indeterminate": set(),
    "transferred": set(),
    "cancelled": set(),
}


class StateStore:
    """Persist handoff state beneath a caller-supplied private directory."""

    def __init__(self, root: Path, now: Callable[[], float]):
        self.root = Path(root)
        self.now = now
        self.pending_dir = self.root / "pending"
        self.sessions_dir = self.root / "sessions"
        self.locks_dir = self.root / "locks"
        for directory in (
            self.root,
            self.pending_dir,
            self.sessions_dir,
            self.locks_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    def prepare(
        self,
        cwd: str,
        handoff_text: str,
        target_path: str,
    ) -> dict[str, object]:
        pending_id = str(uuid.uuid4())
        record: dict[str, object] = {
            "pending_id": pending_id,
            "state": "draft",
            "cwd": cwd,
            "handoff_text": handoff_text,
            "target_path": target_path,
            "created_at": self.now(),
        }
        with self._locked(pending_id):
            self._write_record(self._pending_path(pending_id), record)
        return record

    def arm(
        self,
        pending_id: str,
        session_id: str,
        timeout_seconds: int,
    ) -> dict[str, object] | None:
        if timeout_seconds != 300:
            raise ValueError("timeout_seconds must be exactly 300")
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "draft":
                return None
            self._set_state(record, "armed")
            record["session_id"] = session_id
            record["deadline_at"] = self.now() + timeout_seconds
            self._persist(record)
            return record

    def respond(self, session_id: str) -> dict[str, object] | None:
        session_record = self.get_session_status(session_id)
        if session_record is None or "pending_id" not in session_record:
            return None
        pending_id = str(session_record["pending_id"])
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "armed":
                return None
            if self.now() >= float(record["deadline_at"]):
                return None
            self._set_state(record, "responded")
            record["responded_at"] = self.now()
            self._persist(record)
            return record

    def claim_confirm(self, pending_id: str) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] == "thread_created":
                return record
            if record["state"] not in {"armed", "responded", "failed"}:
                return None
            self._set_state(record, "transferring")
            record["claim_reason"] = "confirmed"
            record["transferring_at"] = self.now()
            self._persist(record)
            return record

    def claim_expired(self, pending_id: str) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "armed":
                return None
            timestamp = self.now()
            if timestamp < float(record["deadline_at"]):
                return None
            self._set_state(record, "expired")
            record["expired_at"] = timestamp
            self._set_state(record, "transferring")
            record["claim_reason"] = "expired"
            record["transferring_at"] = timestamp
            self._persist(record)
            return record

    def mark_thread_starting(
        self,
        pending_id: str,
        recovery_prompt: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "transferring":
                return None
            self._set_state(record, "thread_starting")
            record["recovery_prompt"] = recovery_prompt
            record["thread_starting_at"] = self.now()
            self._persist(record)
            return record

    def mark_thread_created(
        self,
        pending_id: str,
        new_thread_id: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "thread_starting":
                return None
            self._set_state(record, "thread_created")
            record["new_thread_id"] = new_thread_id
            record["thread_created_at"] = self.now()
            self._persist(record)
            return record

    def mark_turn_starting(
        self,
        pending_id: str,
        client_user_message_id: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "thread_created":
                return None
            self._set_state(record, "turn_starting")
            record["client_user_message_id"] = client_user_message_id
            record["turn_starting_at"] = self.now()
            self._persist(record)
            return record

    def mark_transferred(
        self,
        pending_id: str,
        new_thread_id: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "turn_starting":
                return None
            recorded_thread_id = record.get("new_thread_id")
            if recorded_thread_id != new_thread_id:
                raise ValueError("new thread id does not match durable state")
            self._set_state(record, "transferred")
            record["transferred_at"] = self.now()
            self._persist(record)
            return record

    def mark_indeterminate(
        self,
        pending_id: str,
        error_summary: str,
        recovery_prompt: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            state = str(record["state"])
            if state not in {"thread_starting", "turn_starting"}:
                return None
            self._set_state(record, "indeterminate")
            record["external_phase"] = state
            record["error_summary"] = error_summary
            record["recovery_prompt"] = recovery_prompt
            record["indeterminate_at"] = self.now()
            self._persist(record)
            return record

    def mark_failed(
        self,
        pending_id: str,
        error_summary: str,
        recovery_prompt: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "transferring":
                return None
            self._set_state(record, "failed")
            record["error_summary"] = error_summary
            record["recovery_prompt"] = recovery_prompt
            record["failed_at"] = self.now()
            self._persist(record)
            return record

    def cancel(self, pending_id: str) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] == "cancelled":
                return record
            if "cancelled" not in LEGAL_TRANSITIONS[str(record["state"])]:
                return None
            self._set_state(record, "cancelled")
            record["cancelled_at"] = self.now()
            self._persist(record)
            return record

    def record_compaction(self, session_id: str, source: str) -> int:
        with self._session_locked(session_id):
            path = self._session_path(session_id)
            record = (
                self._read_record(path)
                if path.exists()
                else {"session_id": session_id}
            )
            count = int(record.get("compaction_count", 0)) + 1
            sources = dict(record.get("compaction_sources", {}))
            sources[source] = int(sources.get(source, 0)) + 1
            record["compaction_count"] = count
            record["compaction_sources"] = sources
            self._write_record(path, record)
            return count

    def get_compaction_count(self, session_id: str) -> int:
        record = self.get_session_status(session_id)
        if record is None:
            return 0
        return int(record.get("compaction_count", 0))

    def get_session_status(self, session_id: str) -> dict[str, object] | None:
        path = self._session_path(session_id)
        cached = self._read_record(path) if path.exists() else None
        candidates: list[dict[str, object]] = []
        for pending_path in self.pending_dir.glob("*.json"):
            pending_id = pending_path.stem
            try:
                with self._locked(pending_id):
                    if pending_path.exists():
                        record = self._read_record(pending_path)
                        if record.get("session_id") == session_id:
                            candidates.append(record)
            except ValueError:
                continue
        if not candidates:
            return cached

        record = max(candidates, key=self._session_record_rank)
        pending_id = str(record["pending_id"])
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            with self._session_locked(session_id):
                if path.exists():
                    current_cache = self._read_record(path)
                    for key in ("compaction_count", "compaction_sources"):
                        if key in current_cache:
                            record[key] = current_cache[key]
                self._write_record(path, record)
        return record

    def _pending_path(self, pending_id: str) -> Path:
        self._validate_pending_id(pending_id)
        return self.pending_dir / f"{pending_id}.json"

    def _session_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.sessions_dir / f"{digest}.json"

    def _locked(self, pending_id: str):
        self._validate_pending_id(pending_id)
        return _RecordLock(self.locks_dir / f"{pending_id}.lock")

    def _session_locked(self, session_id: str):
        lock_id = uuid.uuid5(uuid.NAMESPACE_URL, f"project-handoff:{session_id}")
        return _RecordLock(self.locks_dir / f"{lock_id}.lock")

    def _persist(self, record: dict[str, object]) -> None:
        session_id = record.get("session_id")
        if isinstance(session_id, str):
            session_path = self._session_path(session_id)
            with self._session_locked(session_id):
                if session_path.exists():
                    session_record = self._read_record(session_path)
                    for key in ("compaction_count", "compaction_sources"):
                        if key in session_record:
                            record[key] = session_record[key]
                self._write_record(self._pending_path(str(record["pending_id"])), record)
                self._write_record(session_path, record)
            return
        self._write_record(self._pending_path(str(record["pending_id"])), record)

    @staticmethod
    def _set_state(record: dict[str, object], new_state: str) -> None:
        state = str(record["state"])
        if new_state not in LEGAL_TRANSITIONS[state]:
            raise ValueError(f"illegal state transition: {state} -> {new_state}")
        record["state"] = new_state

    @staticmethod
    def _session_record_rank(record: dict[str, object]) -> tuple[bool, float, str]:
        active = record.get("state") not in {"transferred", "cancelled"}
        return (
            active,
            float(record.get("created_at", 0.0)),
            str(record["pending_id"]),
        )

    @staticmethod
    def _validate_pending_id(pending_id: str) -> None:
        try:
            parsed = uuid.UUID(pending_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid pending id") from error
        if str(parsed) != pending_id:
            raise ValueError("invalid pending id")

    @staticmethod
    def _read_record(path: Path) -> dict[str, object]:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_record(path: Path, record: dict[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class _RecordLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._handle = os.fdopen(descriptor, "a+")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        assert self._handle is not None
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
