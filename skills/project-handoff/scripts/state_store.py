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
    "thread_starting": {"thread_created", "failed", "indeterminate"},
    "thread_created": {"turn_starting"},
    "turn_starting": {"thread_created", "transferred", "indeterminate"},
    "failed": {"transferring", "cancelled"},
    "indeterminate": set(),
    "transferred": set(),
    "cancelled": set(),
}

_ATTEMPT_DIAGNOSTIC_KEYS = (
    "request_outcome",
    "error_summary",
    "failed_at",
    "turn_unsent_at",
    "external_phase",
    "indeterminate_at",
)
_REQUEST_HISTORY_LIMIT = 20
_SESSION_METADATA_KEYS = (
    "compaction_count",
    "compaction_sources",
    "prompt_fence_generation",
    "prompt_fence_prepare_sequence",
    "prompt_fence_at",
    "prompt_fence_result",
)


class StateAuthorityError(RuntimeError):
    """Concurrent generation churn prevented a stable session decision."""


class StateStore:
    """Persist handoff state beneath a caller-supplied private directory."""

    def __init__(self, root: Path, now: Callable[[], float]):
        self.root = Path(root)
        self.now = now
        self.pending_dir = self.root / "pending"
        self.sessions_dir = self.root / "sessions"
        self.session_metadata_dir = self.root / "session-metadata"
        self.locks_dir = self.root / "locks"
        self.prepare_sequence_path = self.root / "prepare-sequence.json"
        for directory in (
            self.root,
            self.pending_dir,
            self.sessions_dir,
            self.session_metadata_dir,
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
        prepare_sequence = self._allocate_prepare_sequence()
        record: dict[str, object] = {
            "pending_id": pending_id,
            "prepare_sequence": prepare_sequence,
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
        with self._authority_locked(session_id):
            authoritative = self._authoritative_session_record_locked(session_id)
            metadata = self._load_session_metadata_locked(
                session_id,
                authoritative,
            )
            with self._locked(pending_id):
                record = self._read_record(self._pending_path(pending_id))
                if record["state"] != "draft":
                    return None
                self._set_state(record, "armed")
                record["session_id"] = session_id
                record["deadline_at"] = self.now() + timeout_seconds
                if self._record_is_prompt_fenced(record, metadata):
                    self._set_state(record, "cancelled")
                    record["cancelled_at"] = self.now()
                    record["cancel_reason"] = "prompt_fenced"
                    self._copy_session_metadata(metadata, record)
                    self._write_record(
                        self._pending_path(pending_id),
                        record,
                    )
                    return None
                record["generation"] = self._record_generation(authoritative) + 1
                self._copy_session_metadata(metadata, record)
                self._write_record(self._pending_path(pending_id), record)
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), record)
            return record

    def respond(self, session_id: str) -> dict[str, object] | None:
        with self._prepare_sequence_locked():
            fence_sequence = self._latest_prepare_sequence_locked()
            with self._authority_locked(session_id):
                authoritative = self._authoritative_session_record_locked(
                    session_id,
                )
                metadata = self._load_session_metadata_locked(
                    session_id,
                    authoritative,
                )
                self._advance_prompt_fence(metadata, fence_sequence)
                if authoritative is None:
                    metadata["prompt_fence_result"] = "observed"
                    self._write_session_metadata(session_id, metadata)
                    with self._session_locked(session_id):
                        self._write_record(
                            self._session_path(session_id),
                            metadata,
                        )
                    return self._prompt_fence_proof(metadata, "observed")

                pending_id = str(authoritative["pending_id"])
                with self._locked(pending_id):
                    pending_path = self._pending_path(pending_id)
                    record = self._read_record(pending_path)
                    if not self._same_bound_generation(record, authoritative):
                        raise StateAuthorityError(
                            "durable session authority changed unexpectedly"
                        )
                    result = "observed"
                    if record["state"] == "armed":
                        self._set_state(record, "responded")
                        record["responded_at"] = self.now()
                        result = "responded"
                    metadata["prompt_fence_result"] = result
                    self._write_session_metadata(session_id, metadata)
                    self._copy_session_metadata(metadata, record)
                    with self._session_locked(session_id):
                        self._write_record(pending_path, record)
                        self._write_record(
                            self._session_path(session_id),
                            record,
                        )
                        return self._prompt_fence_proof(record, result)

    def claim_confirm(self, pending_id: str) -> dict[str, object] | None:
        initial = self._read_record(self._pending_path(pending_id))
        session_id = initial.get("session_id")
        if not isinstance(session_id, str):
            return None
        with self._authority_locked(session_id):
            authoritative = self._authoritative_session_record_locked(session_id)
            metadata = self._load_session_metadata_locked(
                session_id,
                authoritative,
            )
            with self._locked(pending_id):
                record = self._read_record(self._pending_path(pending_id))
                if not self._same_bound_generation(record, authoritative):
                    self._cancel_superseded_locally(record, metadata)
                    return None
                if record["state"] == "thread_created":
                    return record
                if record["state"] not in {"armed", "responded", "failed"}:
                    return None
                self._set_state(record, "transferring")
                record["claim_reason"] = "confirmed"
                record["transferring_at"] = self.now()
                self._copy_session_metadata(metadata, record)
                self._write_record(self._pending_path(pending_id), record)
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), record)
                return record

    def claim_expired(self, pending_id: str) -> dict[str, object] | None:
        initial = self._read_record(self._pending_path(pending_id))
        session_id = initial.get("session_id")
        if not isinstance(session_id, str):
            return None
        with self._authority_locked(session_id):
            authoritative = self._authoritative_session_record_locked(session_id)
            metadata = self._load_session_metadata_locked(
                session_id,
                authoritative,
            )
            with self._locked(pending_id):
                record = self._read_record(self._pending_path(pending_id))
                if record["state"] != "armed":
                    return None
                if not self._same_bound_generation(record, authoritative):
                    self._cancel_armed_locally(
                        record,
                        "superseded_generation",
                        metadata,
                    )
                    return None
                if self._record_is_prompt_fenced(record, metadata):
                    self._cancel_armed_locally(
                        record,
                        "prompt_fenced",
                        metadata,
                    )
                    return None
                timestamp = self.now()
                if timestamp < float(record["deadline_at"]):
                    return None
                self._set_state(record, "expired")
                record["expired_at"] = timestamp
                self._set_state(record, "transferring")
                record["claim_reason"] = "expired"
                record["transferring_at"] = timestamp
                self._copy_session_metadata(metadata, record)
                self._write_record(self._pending_path(pending_id), record)
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), record)
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
            self._clear_attempt_diagnostics(record)
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
            self._clear_attempt_diagnostics(record)
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
            record["request_outcome"] = "possibly_sent"
            record["external_phase"] = state
            record["error_summary"] = error_summary
            record["recovery_prompt"] = recovery_prompt
            record["indeterminate_at"] = self.now()
            self._persist(record)
            return record

    def mark_thread_unsent_failed(
        self,
        pending_id: str,
        error_summary: str,
        recovery_prompt: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "thread_starting":
                return None
            self._set_state(record, "failed")
            timestamp = self.now()
            self._append_request_history(
                record,
                phase="thread_start",
                outcome="definitely_unsent",
                error_summary=error_summary,
                recorded_at=timestamp,
            )
            record["request_outcome"] = "definitely_unsent"
            record["error_summary"] = error_summary
            record["recovery_prompt"] = recovery_prompt
            record["failed_at"] = timestamp
            self._persist(record)
            return record

    def mark_turn_unsent(
        self,
        pending_id: str,
        error_summary: str,
        recovery_prompt: str,
    ) -> dict[str, object] | None:
        with self._locked(pending_id):
            record = self._read_record(self._pending_path(pending_id))
            if record["state"] != "turn_starting":
                return None
            self._set_state(record, "thread_created")
            timestamp = self.now()
            self._append_request_history(
                record,
                phase="turn_start",
                outcome="definitely_unsent",
                error_summary=error_summary,
                recorded_at=timestamp,
            )
            record["request_outcome"] = "definitely_unsent"
            record["error_summary"] = error_summary
            record["recovery_prompt"] = recovery_prompt
            record["turn_unsent_at"] = timestamp
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
        with self._authority_locked(session_id):
            authoritative = self._authoritative_session_record_locked(session_id)
            metadata = self._load_session_metadata_locked(
                session_id,
                authoritative,
            )
            count = int(metadata.get("compaction_count", 0)) + 1
            sources = dict(metadata.get("compaction_sources", {}))
            sources[source] = int(sources.get(source, 0)) + 1
            metadata["compaction_count"] = count
            metadata["compaction_sources"] = sources
            self._write_session_metadata(session_id, metadata)
            if authoritative is None:
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), metadata)
                return count

            pending_id = str(authoritative["pending_id"])
            with self._locked(pending_id):
                record = self._read_record(self._pending_path(pending_id))
                if not self._same_bound_generation(record, authoritative):
                    raise StateAuthorityError(
                        "durable session authority changed unexpectedly"
                    )
                self._copy_session_metadata(metadata, record)
                self._write_record(self._pending_path(pending_id), record)
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), record)
            return count

    def get_compaction_count(self, session_id: str) -> int:
        record = self.get_session_status(session_id)
        if record is None:
            return 0
        return int(record.get("compaction_count", 0))

    def get_session_status(self, session_id: str) -> dict[str, object] | None:
        with self._authority_locked(session_id):
            authoritative = self._authoritative_session_record_locked(session_id)
            metadata = self._load_session_metadata_locked(
                session_id,
                authoritative,
            )
            if authoritative is None:
                if not self._metadata_has_values(metadata):
                    return None
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), metadata)
                return metadata

            pending_id = str(authoritative["pending_id"])
            with self._locked(pending_id):
                record = self._read_record(self._pending_path(pending_id))
                if not self._same_bound_generation(record, authoritative):
                    raise StateAuthorityError(
                        "durable session authority changed unexpectedly"
                    )
                self._copy_session_metadata(metadata, record)
                with self._session_locked(session_id):
                    self._write_record(self._session_path(session_id), record)
                return record

    def _session_candidates(
        self,
        session_id: str,
    ) -> list[dict[str, object]]:
        candidates: list[dict[str, object]] = []
        for pending_path in self.pending_dir.glob("*.json"):
            pending_id = pending_path.stem
            try:
                self._validate_pending_id(pending_id)
            except ValueError:
                continue
            with self._locked(pending_id):
                if pending_path.exists():
                    record = self._read_record(pending_path)
                    if record.get("session_id") == session_id:
                        candidates.append(record)
        return candidates

    def _pending_path(self, pending_id: str) -> Path:
        self._validate_pending_id(pending_id)
        return self.pending_dir / f"{pending_id}.json"

    def _session_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.sessions_dir / f"{digest}.json"

    def _session_metadata_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.session_metadata_dir / f"{digest}.json"

    def _locked(self, pending_id: str):
        self._validate_pending_id(pending_id)
        return _RecordLock(self.locks_dir / f"{pending_id}.lock")

    def _session_locked(self, session_id: str):
        lock_id = uuid.uuid5(uuid.NAMESPACE_URL, f"project-handoff:{session_id}")
        return _RecordLock(self.locks_dir / f"{lock_id}.lock")

    def _authority_locked(self, session_id: str):
        lock_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"project-handoff-authority:{session_id}",
        )
        return _RecordLock(self.locks_dir / f"authority-{lock_id}.lock")

    def _persist(self, record: dict[str, object]) -> None:
        session_id = record.get("session_id")
        if isinstance(session_id, str):
            session_path = self._session_path(session_id)
            metadata_path = self._session_metadata_path(session_id)
            metadata = (
                self._read_record(metadata_path)
                if metadata_path.exists()
                else None
            )
            with self._session_locked(session_id):
                session_record = (
                    self._read_record(session_path)
                    if session_path.exists()
                    else None
                )
                self._copy_session_metadata(
                    metadata if metadata is not None else session_record,
                    record,
                )
                self._write_record(self._pending_path(str(record["pending_id"])), record)
                if (
                    session_record is None
                    or self._session_record_rank(record)
                    >= self._session_record_rank(session_record)
                ):
                    self._write_record(session_path, record)
            return
        self._write_record(self._pending_path(str(record["pending_id"])), record)

    def _authoritative_session_record_locked(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        candidates = self._session_candidates(session_id)
        if not candidates:
            return None
        return max(candidates, key=self._session_record_rank)

    def _load_session_metadata_locked(
        self,
        session_id: str,
        authoritative: dict[str, object] | None,
    ) -> dict[str, object]:
        metadata_path = self._session_metadata_path(session_id)
        if metadata_path.exists():
            return self._read_record(metadata_path)

        session_path = self._session_path(session_id)
        cached = self._read_record(session_path) if session_path.exists() else None
        metadata: dict[str, object] = {"session_id": session_id}
        legacy_records = [
            candidate
            for candidate in (authoritative, cached)
            if candidate is not None
        ]
        if legacy_records:
            compaction_source = max(
                legacy_records,
                key=lambda candidate: self._nonnegative_int(
                    candidate.get("compaction_count")
                ),
            )
            self._copy_metadata_keys(
                compaction_source,
                metadata,
                ("compaction_count", "compaction_sources"),
            )
            fence_source = max(
                legacy_records,
                key=lambda candidate: self._nonnegative_int(
                    candidate.get("prompt_fence_generation")
                ),
            )
            self._copy_metadata_keys(
                fence_source,
                metadata,
                (
                    "prompt_fence_generation",
                    "prompt_fence_prepare_sequence",
                    "prompt_fence_at",
                    "prompt_fence_result",
                ),
            )
        if self._metadata_has_values(metadata):
            self._write_session_metadata(session_id, metadata)
        return metadata

    def _write_session_metadata(
        self,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        durable = {"session_id": session_id}
        self._copy_session_metadata(metadata, durable)
        self._write_record(self._session_metadata_path(session_id), durable)

    @staticmethod
    def _metadata_has_values(metadata: dict[str, object]) -> bool:
        return any(key in metadata for key in _SESSION_METADATA_KEYS)

    @staticmethod
    def _copy_metadata_keys(
        source: dict[str, object],
        destination: dict[str, object],
        keys: tuple[str, ...],
    ) -> None:
        for key in keys:
            if key in source:
                destination[key] = source[key]

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(value, 0)
        return 0

    @staticmethod
    def _copy_session_metadata(
        source: dict[str, object] | None,
        destination: dict[str, object],
    ) -> None:
        if source is None:
            return
        for key in _SESSION_METADATA_KEYS:
            if key in source:
                destination[key] = source[key]

    def _allocate_prepare_sequence(self) -> int:
        with self._prepare_sequence_locked():
            sequence = self._latest_prepare_sequence_locked() + 1
            self._write_record(
                self.prepare_sequence_path,
                {"latest_prepare_sequence": sequence},
            )
            return sequence

    def _latest_prepare_sequence_locked(self) -> int:
        sequence = 0
        if self.prepare_sequence_path.exists():
            metadata = self._read_record(self.prepare_sequence_path)
            value = metadata.get("latest_prepare_sequence", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                sequence = max(sequence, value)
        for pending_path in self.pending_dir.glob("*.json"):
            try:
                self._validate_pending_id(pending_path.stem)
            except ValueError:
                continue
            record = self._read_record(pending_path)
            value = record.get("prepare_sequence", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                sequence = max(sequence, value)
        return sequence

    def _prepare_sequence_locked(self):
        return _RecordLock(self.locks_dir / "prepare-sequence.lock")

    def _advance_prompt_fence(
        self,
        record: dict[str, object],
        prepare_sequence: int,
    ) -> None:
        generation = record.get("prompt_fence_generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = 0
        record["prompt_fence_generation"] = generation + 1
        record["prompt_fence_prepare_sequence"] = prepare_sequence
        record["prompt_fence_at"] = float(self.now())

    @staticmethod
    def _prompt_fence_proof(
        record: dict[str, object],
        result: str,
    ) -> dict[str, object]:
        proof = dict(record)
        proof["prompt_fence_result"] = result
        return proof

    def _record_is_prompt_fenced(
        self,
        record: dict[str, object],
        session_record: dict[str, object] | None,
    ) -> bool:
        if session_record is None:
            return False
        cutoff = session_record.get("prompt_fence_prepare_sequence")
        if not isinstance(cutoff, int) or isinstance(cutoff, bool):
            return False
        sequence = record.get("prepare_sequence", 0)
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            sequence = 0
        return sequence <= cutoff

    def _cancel_armed_locally(
        self,
        record: dict[str, object],
        reason: str,
        session_record: dict[str, object] | None,
    ) -> None:
        self._set_state(record, "cancelled")
        record["cancelled_at"] = self.now()
        record["cancel_reason"] = reason
        self._copy_session_metadata(session_record, record)
        self._write_record(
            self._pending_path(str(record["pending_id"])),
            record,
        )

    def _cancel_superseded_locally(
        self,
        record: dict[str, object],
        session_metadata: dict[str, object],
    ) -> None:
        if "cancelled" not in LEGAL_TRANSITIONS[str(record["state"])]:
            return
        self._set_state(record, "cancelled")
        record["cancelled_at"] = self.now()
        record["cancel_reason"] = "superseded_generation"
        self._copy_session_metadata(session_metadata, record)
        self._write_record(
            self._pending_path(str(record["pending_id"])),
            record,
        )

    @staticmethod
    def _set_state(record: dict[str, object], new_state: str) -> None:
        state = str(record["state"])
        if new_state not in LEGAL_TRANSITIONS[state]:
            raise ValueError(f"illegal state transition: {state} -> {new_state}")
        record["state"] = new_state

    @staticmethod
    def _clear_attempt_diagnostics(record: dict[str, object]) -> None:
        for key in _ATTEMPT_DIAGNOSTIC_KEYS:
            record.pop(key, None)

    @staticmethod
    def _append_request_history(
        record: dict[str, object],
        phase: str,
        outcome: str,
        error_summary: str,
        recorded_at: float,
    ) -> None:
        existing = record.get("request_history")
        history = list(existing) if isinstance(existing, list) else []
        history.append(
            {
                "phase": phase,
                "outcome": outcome,
                "error_summary": error_summary,
                "recorded_at": recorded_at,
            }
        )
        record["request_history"] = history[-_REQUEST_HISTORY_LIMIT:]

    @staticmethod
    def _session_record_rank(record: dict[str, object]) -> tuple[int, float, str]:
        return (
            StateStore._record_generation(record),
            float(record.get("created_at", 0.0)),
            str(record.get("pending_id", "")),
        )

    @staticmethod
    def _record_generation(record: dict[str, object] | None) -> int:
        if record is None:
            return 0
        generation = record.get("generation", 0)
        if isinstance(generation, int) and not isinstance(generation, bool):
            return max(generation, 0)
        return 0

    @staticmethod
    def _same_bound_generation(
        left: dict[str, object] | None,
        right: dict[str, object] | None,
    ) -> bool:
        if left is None or right is None:
            return False
        return (
            left.get("pending_id") == right.get("pending_id")
            and StateStore._record_generation(left)
            == StateStore._record_generation(right)
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
