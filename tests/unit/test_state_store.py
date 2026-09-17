import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "project-handoff"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

from state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def read_pending(self, pending_id):
        path = self.root / "pending" / f"{pending_id}.json"
        return json.loads(path.read_text())

    def read_session(self, session_id):
        session_name = hashlib.sha256(session_id.encode()).hexdigest()
        path = self.root / "sessions" / f"{session_name}.json"
        return json.loads(path.read_text())

    def run_race(self, *operations):
        barrier = threading.Barrier(len(operations) + 1)
        results = [None] * len(operations)
        errors = []

        def worker(index, operation):
            try:
                barrier.wait()
                results[index] = operation()
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=worker, args=(index, operation))
            for index, operation in enumerate(operations)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_prepare_then_arm_binds_session_and_deadline(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare(
            cwd="/workspace/repo",
            handoff_text="# Current Focus\nShip it\n",
            target_path="/workspace/repo/docs/AI-HANDOFF.md",
        )

        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)

        self.assertEqual(armed["state"], "armed")
        self.assertEqual(armed["session_id"], "thr-old")
        self.assertEqual(armed["deadline_at"], 400.0)
        pending_path = self.root / "pending" / f'{record["pending_id"]}.json'
        self.assertEqual(json.loads(pending_path.read_text()), armed)
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        session_path = self.root / "sessions" / f"{session_name}.json"
        self.assertEqual(json.loads(session_path.read_text()), armed)
        self.assertEqual(stat.S_IMODE(pending_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(session_path.stat().st_mode), 0o600)
        for directory in ("pending", "sessions", "locks"):
            self.assertEqual(
                stat.S_IMODE((self.root / directory).stat().st_mode),
                0o700,
            )

    def test_arm_rejects_every_timeout_except_five_minutes(self):
        store = StateStore(self.root, now=lambda: 100.0)

        for timeout_seconds in (0, -1, 1, 299, 301):
            with self.subTest(timeout_seconds=timeout_seconds):
                record = store.prepare(
                    "/repo",
                    "handoff",
                    "/repo/AI-HANDOFF.md",
                )
                session_id = f"thr-{timeout_seconds}"

                with self.assertRaisesRegex(
                    ValueError,
                    "timeout_seconds must be exactly 300",
                ):
                    store.arm(
                        record["pending_id"],
                        session_id,
                        timeout_seconds=timeout_seconds,
                    )

                self.assertEqual(self.read_pending(record["pending_id"]), record)
                self.assertIsNone(store.get_session_status(session_id))

    def test_any_response_wins_against_expiry(self):
        clock = [100.0]
        store = StateStore(self.root, now=lambda: clock[0])
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        clock[0] = 399.0

        results = self.run_race(
            lambda: store.respond("thr-old"),
            lambda: store.claim_expired(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        responded = next(result for result in results if result is not None)
        self.assertEqual(responded["state"], "responded")
        self.assertEqual(self.read_pending(record["pending_id"]), responded)
        self.assertEqual(self.read_session("thr-old"), responded)

    def test_session_lookup_recovers_after_interrupted_cache_write(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        (self.root / "sessions" / f"{session_name}.json").unlink()

        recovered = store.get_session_status("thr-old")
        responded = store.respond("thr-old")

        self.assertEqual(recovered, armed)
        self.assertIsNotNone(responded)
        self.assertEqual(responded["state"], "responded")
        self.assertEqual(responded["pending_id"], armed["pending_id"])
        self.assertEqual(self.read_pending(record["pending_id"]), responded)
        self.assertEqual(self.read_session("thr-old"), responded)

    def test_session_lookup_recovers_stale_cache_after_interrupted_update(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        armed = store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        claimed = store.claim_confirm(record["pending_id"])
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        session_path = self.root / "sessions" / f"{session_name}.json"
        session_path.write_text(json.dumps(armed))

        recovered = store.get_session_status("thr-old")

        self.assertEqual(recovered, claimed)
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_session_lookup_does_not_hide_corrupt_pending_json(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", 300)
        pending_path = self.root / "pending" / f"{record['pending_id']}.json"
        pending_path.write_text("{ corrupt private state")

        with self.assertRaises(json.JSONDecodeError):
            store.get_session_status("thr-old")

    def test_newest_session_generation_wins_even_when_older_record_is_active(self):
        store = StateStore(self.root, now=lambda: 100.0)
        store.record_compaction("thr-old", "auto")
        older = store.prepare("/repo", "old", "/repo/old.md")
        older_armed = store.arm(older["pending_id"], "thr-old", 300)
        store.claim_confirm(older["pending_id"])
        store.mark_failed(
            older["pending_id"],
            error_summary="older retryable failure",
            recovery_prompt="retry old",
        )

        newer = store.prepare("/repo", "new", "/repo/new.md")
        newer_armed = store.arm(newer["pending_id"], "thr-old", 300)
        store.claim_confirm(newer["pending_id"])
        store.mark_thread_starting(newer["pending_id"], "resume new")
        store.mark_thread_created(newer["pending_id"], "thr-new")
        store.mark_turn_starting(
            newer["pending_id"],
            f"project-handoff:{newer['pending_id']}",
        )
        store.mark_transferred(newer["pending_id"], "thr-new")

        status = store.get_session_status("thr-old")

        self.assertEqual(older_armed["generation"], 1)
        self.assertEqual(newer_armed["generation"], 2)
        self.assertEqual(status["pending_id"], newer["pending_id"])
        self.assertEqual(status["state"], "transferred")
        self.assertEqual(status["generation"], 2)
        self.assertEqual(status["compaction_count"], 1)
        self.assertEqual(status["compaction_sources"], {"auto": 1})

    def test_cache_rebuild_keeps_compaction_metadata_on_newest_generation(self):
        store = StateStore(self.root, now=lambda: 100.0)
        first = store.prepare("/repo", "first", "/repo/first.md")
        store.arm(first["pending_id"], "thr-old", 300)
        second = store.prepare("/repo", "second", "/repo/second.md")
        store.arm(second["pending_id"], "thr-old", 300)
        stale = self.read_pending(first["pending_id"])
        stale["compaction_count"] = 3
        stale["compaction_sources"] = {"auto": 2, "manual": 1}
        session_name = hashlib.sha256(b"thr-old").hexdigest()
        session_path = self.root / "sessions" / f"{session_name}.json"
        session_path.write_text(json.dumps(stale))

        rebuilt = store.get_session_status("thr-old")

        self.assertEqual(rebuilt["pending_id"], second["pending_id"])
        self.assertEqual(rebuilt["generation"], 2)
        self.assertEqual(rebuilt["compaction_count"], 3)
        self.assertEqual(
            rebuilt["compaction_sources"],
            {"auto": 2, "manual": 1},
        )
        self.assertEqual(self.read_session("thr-old"), rebuilt)

    def test_concurrent_arms_assign_distinct_monotonic_generations(self):
        store = StateStore(self.root, now=lambda: 100.0)
        first = store.prepare("/repo", "first", "/repo/first.md")
        second = store.prepare("/repo", "second", "/repo/second.md")

        armed = self.run_race(
            lambda: store.arm(first["pending_id"], "thr-old", 300),
            lambda: store.arm(second["pending_id"], "thr-old", 300),
        )

        self.assertEqual({record["generation"] for record in armed}, {1, 2})
        status = store.get_session_status("thr-old")
        self.assertEqual(status["generation"], 2)
        generation_two = next(
            record for record in armed if record["generation"] == 2
        )
        self.assertEqual(status["pending_id"], generation_two["pending_id"])

    def test_status_never_downgrades_cache_when_new_arm_wins_scan_race(self):
        store = StateStore(self.root, now=lambda: 100.0)
        older = store.prepare("/repo", "older", "/repo/older.md")
        store.arm(older["pending_id"], "thr-old", 300)
        newer = store.prepare("/repo", "newer", "/repo/newer.md")
        paused = threading.Event()
        resume = threading.Event()
        original_session_locked = store._session_locked

        class PausingSessionLock:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                paused.set()
                if not resume.wait(timeout=5):
                    raise RuntimeError("status race barrier timed out")
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        def controlled_session_lock(session_id):
            lock = original_session_locked(session_id)
            if threading.current_thread().name == "status-race":
                return PausingSessionLock(lock)
            return lock

        store._session_locked = controlled_session_lock
        result = []
        errors = []

        def lookup():
            try:
                result.append(store.get_session_status("thr-old"))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=lookup, name="status-race")
        thread.start()
        self.assertTrue(paused.wait(timeout=5))
        newer_armed = store.arm(newer["pending_id"], "thr-old", 300)
        resume.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["pending_id"], newer["pending_id"])
        self.assertEqual(result[0]["generation"], newer_armed["generation"])
        self.assertEqual(
            self.read_session("thr-old")["pending_id"],
            newer["pending_id"],
        )

    def test_respond_retries_when_new_arm_wins_authority_window(self):
        store = StateStore(self.root, now=lambda: 100.0)
        older = store.prepare("/repo", "older", "/repo/older.md")
        store.arm(older["pending_id"], "thr-old", 300)
        newer = store.prepare("/repo", "newer", "/repo/newer.md")
        paused = threading.Event()
        resume = threading.Event()
        original_session_locked = store._session_locked
        respond_lock_count = 0
        count_guard = threading.Lock()

        class PausingSessionLock:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                paused.set()
                if not resume.wait(timeout=5):
                    raise RuntimeError("respond race barrier timed out")
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        def controlled_session_lock(session_id):
            nonlocal respond_lock_count
            lock = original_session_locked(session_id)
            if threading.current_thread().name != "respond-race":
                return lock
            with count_guard:
                respond_lock_count += 1
                should_pause = respond_lock_count == 2
            return PausingSessionLock(lock) if should_pause else lock

        store._session_locked = controlled_session_lock
        result = []
        errors = []

        def respond():
            try:
                result.append(store.respond("thr-old"))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=respond, name="respond-race")
        thread.start()
        self.assertTrue(paused.wait(timeout=5))
        newer_armed = store.arm(newer["pending_id"], "thr-old", 300)
        resume.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["pending_id"], newer["pending_id"])
        self.assertEqual(result[0]["generation"], newer_armed["generation"])
        self.assertEqual(result[0]["state"], "responded")
        self.assertEqual(store.get_session_status("thr-old"), result[0])

    def test_expiry_wins_against_late_response(self):
        clock = [100.0]
        store = StateStore(self.root, now=lambda: clock[0])
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        clock[0] = 401.0

        results = self.run_race(
            lambda: store.respond("thr-old"),
            lambda: store.claim_expired(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result is not None)
        self.assertEqual(claimed["state"], "transferring")
        self.assertEqual(claimed["claim_reason"], "expired")
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_confirm_can_claim_responded_record_once(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.respond("thr-old")

        results = self.run_race(
            lambda: store.claim_confirm(record["pending_id"]),
            lambda: store.claim_confirm(record["pending_id"]),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result is not None)
        self.assertEqual(claimed["state"], "transferring")
        self.assertEqual(claimed["claim_reason"], "confirmed")
        self.assertEqual(self.read_pending(record["pending_id"]), claimed)
        self.assertEqual(self.read_session("thr-old"), claimed)

    def test_cancel_is_idempotent(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.respond("thr-old")

        cancelled = store.cancel(record["pending_id"])
        cancelled_again = store.cancel(record["pending_id"])

        self.assertEqual(cancelled_again, cancelled)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.read_pending(record["pending_id"]), cancelled)
        self.assertEqual(self.read_session("thr-old"), cancelled)

    def test_transferred_session_points_to_new_thread(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])
        store.mark_thread_starting(
            record["pending_id"],
            recovery_prompt="Inspect the thread/start outcome before retrying.",
        )
        store.mark_thread_created(record["pending_id"], "thr-new")
        store.mark_turn_starting(
            record["pending_id"],
            client_user_message_id=f'project-handoff:{record["pending_id"]}',
        )

        transferred = store.mark_transferred(record["pending_id"], "thr-new")

        self.assertEqual(transferred["state"], "transferred")
        self.assertEqual(transferred["new_thread_id"], "thr-new")
        self.assertEqual(store.get_session_status("thr-old"), transferred)
        self.assertEqual(self.read_pending(record["pending_id"]), transferred)
        self.assertEqual(self.read_session("thr-old"), transferred)

    def test_failed_record_keeps_recovery_prompt(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])

        failed = store.mark_failed(
            record["pending_id"],
            error_summary="app server unavailable",
            recovery_prompt="Open /new and paste this complete recovery prompt.",
        )

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            failed["recovery_prompt"],
            "Open /new and paste this complete recovery prompt.",
        )
        self.assertEqual(self.read_pending(record["pending_id"]), failed)
        retried = store.claim_confirm(record["pending_id"])
        self.assertEqual(retried["state"], "transferring")
        self.assertEqual(retried["recovery_prompt"], failed["recovery_prompt"])
        self.assertEqual(self.read_pending(record["pending_id"]), retried)
        self.assertEqual(self.read_session("thr-old"), retried)

    def test_external_effect_phases_persist_recovery_information(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])

        thread_starting = store.mark_thread_starting(
            record["pending_id"],
            recovery_prompt="Inspect thread creation manually.",
        )
        thread_created = store.mark_thread_created(
            record["pending_id"],
            "thr-new",
        )
        turn_starting = store.mark_turn_starting(
            record["pending_id"],
            client_user_message_id=f'project-handoff:{record["pending_id"]}',
        )
        transferred = store.mark_transferred(record["pending_id"], "thr-new")

        self.assertEqual(thread_starting["state"], "thread_starting")
        self.assertEqual(
            thread_starting["recovery_prompt"],
            "Inspect thread creation manually.",
        )
        self.assertEqual(thread_created["state"], "thread_created")
        self.assertEqual(thread_created["new_thread_id"], "thr-new")
        self.assertEqual(turn_starting["state"], "turn_starting")
        self.assertEqual(
            turn_starting["client_user_message_id"],
            f'project-handoff:{record["pending_id"]}',
        )
        self.assertEqual(transferred["state"], "transferred")

    def test_confirm_resumes_thread_created_but_refuses_ambiguous_phases(self):
        for phase in ("thread_starting", "turn_starting", "indeterminate"):
            with self.subTest(phase=phase):
                case_root = self.root / phase
                store = StateStore(case_root, now=lambda: 100.0)
                record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
                store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
                store.claim_confirm(record["pending_id"])
                store.mark_thread_starting(
                    record["pending_id"],
                    recovery_prompt="Inspect before retrying.",
                )
                if phase in {"turn_starting", "indeterminate"}:
                    store.mark_thread_created(record["pending_id"], "thr-new")
                    store.mark_turn_starting(
                        record["pending_id"],
                        client_user_message_id=(
                            f'project-handoff:{record["pending_id"]}'
                        ),
                    )
                if phase == "indeterminate":
                    store.mark_indeterminate(
                        record["pending_id"],
                        error_summary="turn/start response was lost",
                        recovery_prompt="Inspect the existing thread manually.",
                    )

                self.assertIsNone(store.claim_confirm(record["pending_id"]))

        resumable_store = StateStore(self.root / "resumable", now=lambda: 100.0)
        resumable = resumable_store.prepare(
            "/repo",
            "handoff",
            "/repo/AI-HANDOFF.md",
        )
        resumable_store.arm(
            resumable["pending_id"],
            "thr-old",
            timeout_seconds=300,
        )
        resumable_store.claim_confirm(resumable["pending_id"])
        resumable_store.mark_thread_starting(
            resumable["pending_id"],
            recovery_prompt="Inspect before retrying.",
        )
        resumable_store.mark_thread_created(resumable["pending_id"], "thr-new")

        claimed = resumable_store.claim_confirm(resumable["pending_id"])

        self.assertEqual(claimed["state"], "thread_created")
        self.assertEqual(claimed["new_thread_id"], "thr-new")

    def test_proven_unsent_thread_request_returns_to_retryable_failed(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])
        store.mark_thread_starting(
            record["pending_id"],
            recovery_prompt="Resume the full transfer.",
        )

        failed = store.mark_thread_unsent_failed(
            record["pending_id"],
            error_summary="thread/start was definitely not sent",
            recovery_prompt="Resume the full transfer.",
        )

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["request_outcome"], "definitely_unsent")
        self.assertEqual(
            failed["error_summary"],
            "thread/start was definitely not sent",
        )
        retried = store.claim_confirm(record["pending_id"])
        self.assertEqual(retried["state"], "transferring")

    def test_proven_unsent_turn_request_returns_to_thread_created(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])
        store.mark_thread_starting(
            record["pending_id"],
            recovery_prompt="Resume only the turn.",
        )
        store.mark_thread_created(record["pending_id"], "thr-new")
        store.mark_turn_starting(
            record["pending_id"],
            client_user_message_id=f'project-handoff:{record["pending_id"]}',
        )

        resumable = store.mark_turn_unsent(
            record["pending_id"],
            error_summary="turn/start was definitely not sent",
            recovery_prompt="Resume only the turn.",
        )

        self.assertEqual(resumable["state"], "thread_created")
        self.assertEqual(resumable["new_thread_id"], "thr-new")
        self.assertEqual(resumable["request_outcome"], "definitely_unsent")
        self.assertEqual(
            resumable["recovery_prompt"],
            "Resume only the turn.",
        )
        claimed = store.claim_confirm(record["pending_id"])
        self.assertEqual(claimed["state"], "thread_created")

    def test_new_thread_attempt_clears_prior_unsent_metadata(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])
        store.mark_thread_starting(record["pending_id"], "old prompt")
        store.mark_thread_unsent_failed(
            record["pending_id"],
            error_summary="old definitely-unsent failure",
            recovery_prompt="old prompt",
        )
        store.claim_confirm(record["pending_id"])

        retrying = store.mark_thread_starting(
            record["pending_id"],
            recovery_prompt="new prompt",
        )

        self.assertEqual(retrying["state"], "thread_starting")
        self.assertEqual(retrying["recovery_prompt"], "new prompt")
        self.assertNotIn("request_outcome", retrying)
        self.assertNotIn("error_summary", retrying)
        self.assertNotIn("failed_at", retrying)
        self.assertEqual(
            retrying["request_history"][-1]["outcome"],
            "definitely_unsent",
        )

        ambiguous = store.mark_indeterminate(
            record["pending_id"],
            error_summary="new ambiguous failure",
            recovery_prompt="new prompt",
        )
        self.assertEqual(ambiguous["request_outcome"], "possibly_sent")
        self.assertEqual(ambiguous["error_summary"], "new ambiguous failure")

    def test_new_turn_attempt_clears_prior_unsent_metadata(self):
        store = StateStore(self.root, now=lambda: 100.0)
        record = store.prepare("/repo", "handoff", "/repo/AI-HANDOFF.md")
        store.arm(record["pending_id"], "thr-old", timeout_seconds=300)
        store.claim_confirm(record["pending_id"])
        store.mark_thread_starting(record["pending_id"], "resume prompt")
        store.mark_thread_created(record["pending_id"], "thr-new")
        store.mark_turn_starting(
            record["pending_id"],
            client_user_message_id=f'project-handoff:{record["pending_id"]}',
        )
        store.mark_turn_unsent(
            record["pending_id"],
            error_summary="old definitely-unsent turn failure",
            recovery_prompt="resume prompt",
        )

        retrying = store.mark_turn_starting(
            record["pending_id"],
            client_user_message_id=f'project-handoff:{record["pending_id"]}',
        )

        self.assertEqual(retrying["state"], "turn_starting")
        self.assertEqual(retrying["new_thread_id"], "thr-new")
        self.assertEqual(retrying["recovery_prompt"], "resume prompt")
        self.assertNotIn("request_outcome", retrying)
        self.assertNotIn("error_summary", retrying)
        self.assertNotIn("turn_unsent_at", retrying)
        self.assertEqual(
            retrying["request_history"][-1]["outcome"],
            "definitely_unsent",
        )

        ambiguous = store.mark_indeterminate(
            record["pending_id"],
            error_summary="new ambiguous turn failure",
            recovery_prompt="resume prompt",
        )
        self.assertEqual(ambiguous["request_outcome"], "possibly_sent")
        self.assertEqual(ambiguous["new_thread_id"], "thr-new")

    def test_invalid_pending_id_is_rejected(self):
        store = StateStore(self.root, now=lambda: 100.0)

        with self.assertRaisesRegex(ValueError, "invalid pending id"):
            store.claim_confirm("../../outside")

        self.assertEqual(list((self.root / "pending").iterdir()), [])
        self.assertEqual(list((self.root / "locks").iterdir()), [])

    def test_compaction_counts_are_per_session(self):
        store = StateStore(self.root, now=lambda: 100.0)

        first = store.record_compaction("thr-a", "manual")
        second = store.record_compaction("thr-a", "auto")
        other = store.record_compaction("thr-b", "auto")

        self.assertEqual((first, second, other), (1, 2, 1))
        self.assertEqual(store.get_compaction_count("thr-a"), 2)
        self.assertEqual(store.get_compaction_count("thr-b"), 1)
        self.assertEqual(store.get_compaction_count("thr-missing"), 0)
        self.assertEqual(
            self.read_session("thr-a"),
            {
                "session_id": "thr-a",
                "compaction_count": 2,
                "compaction_sources": {"auto": 1, "manual": 1},
            },
        )
        self.assertEqual(
            self.read_session("thr-b"),
            {
                "session_id": "thr-b",
                "compaction_count": 1,
                "compaction_sources": {"auto": 1},
            },
        )
